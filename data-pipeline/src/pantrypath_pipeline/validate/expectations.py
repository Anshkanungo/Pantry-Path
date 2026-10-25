"""Great Expectations suites for the two reference datasets.

Each expectation has a severity. "critical" failures block the run: an error alert
is sent and ValidationFailedError stops the DAG before bad data is loaded or
versioned. "warning" failures only send a warning alert. Every suite's result is
saved as JSON in reports/ge/<suite>.json, so data quality can be inspected later.

GE checks columns of one DataFrame at a time, so per-row facts that live inside
lists or nested JSON (allergen tags, a dish's ingredients) are flattened into
small DataFrames first, with DuckDB or plain Python.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import great_expectations as gx
import pandas as pd
from great_expectations import expectations as gxe
from great_expectations.data_context.types.base import ProgressBarsConfig

from pantrypath_core.catalog import Cuisine, Role
from pantrypath_core.schema import Category
from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import (
    DataCorruptError,
    DataUnavailableError,
    ValidationFailedError,
)

log = logging.getLogger(__name__)
logging.getLogger("great_expectations").setLevel(logging.WARNING)  # GE resets it on import

CRITICAL = "critical"
WARNING = "warning"

OFF_COLUMNS = [
    "barcode", "code", "product_name", "brands", "categories_tags", "allergens_tags",
    "traces_tags", "ingredients_text", "labels_tags", "ingredients_analysis_tags",
    "unique_scans_n", "completeness", "last_modified_t",
]
GTIN13 = r"^\d{13}$"
OFF_TAG = r"^[a-z]{2,3}:\S"  # language prefix + tag, e.g. "en:milk"
SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def new_context():
    """A throwaway in-memory GE context (no files, no GX Cloud, no progress bars)."""
    context = gx.get_context(mode="ephemeral")
    context.variables.progress_bars = ProgressBarsConfig(globally=False)
    return context


def validate_frame(context, df: pd.DataFrame, suite_name: str,
                   expectations: list) -> dict[str, Any]:
    """Run one GE suite on one DataFrame and return a plain, JSON-ready report."""
    batch = (
        context.data_sources.add_pandas(f"{suite_name}_source")
        .add_dataframe_asset(suite_name)
        .add_batch_definition_whole_dataframe("whole_frame")
        .get_batch(batch_parameters={"dataframe": df})
    )
    suite = gx.ExpectationSuite(name=suite_name)
    for expectation in expectations:
        suite.add_expectation(expectation)
    ge_result = batch.validate(suite, result_format="SUMMARY").to_json_dict()

    checks = []
    for res in ge_result["results"]:
        conf, result = res["expectation_config"], res.get("result") or {}
        exception = res.get("exception_info") or {}
        checks.append({
            "expectation": conf["type"],
            "column": conf["kwargs"].get("column") or conf["kwargs"].get("column_list"),
            "severity": conf.get("severity", CRITICAL),
            "success": bool(res["success"]),
            "observed_value": result.get("observed_value"),
            "unexpected_count": result.get("unexpected_count"),
            "unexpected_percent": result.get("unexpected_percent"),
            "examples": (result.get("partial_unexpected_list") or [])[:5],
            "error": exception.get("exception_message") if exception.get("raised_exception")
            else None,
        })
    return {
        "suite": suite_name,
        "validated_at": datetime.now(UTC).isoformat(),
        "rows": len(df),
        "success": all(c["success"] for c in checks if c["severity"] == CRITICAL),
        "critical_failures": sum(not c["success"] and c["severity"] == CRITICAL for c in checks),
        "warnings": sum(not c["success"] and c["severity"] == WARNING for c in checks),
        "checks": checks,
        "great_expectations": ge_result,
    }


def describe_failure(suite: str, check: dict) -> str:
    """One readable line per failed expectation, for alerts and error messages."""
    column = f"({check['column']})" if check["column"] else ""
    if check["error"]:
        detail = f"could not run: {check['error']}"
    elif check["unexpected_count"] is not None:
        detail = (f"{check['unexpected_count']} bad values "
                  f"({check['unexpected_percent'] or 0:.3g}%), e.g. {check['examples'][:3]}")
    else:
        detail = f"observed {check['observed_value']}"
    return f"{suite}: {check['expectation']}{column} failed: {detail}"


def finish(cfg: Config, dataset: str, reports: list[dict]) -> dict[str, Any]:
    """Save every suite's report, alert on failures, and stop the run on critical ones."""
    results_dir = cfg.path(cfg["validation"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    for report in reports:
        path = results_dir / f"{report['suite']}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
        log.info("%s: %d/%d checks passed, saved %s", report["suite"],
                 sum(c["success"] for c in report["checks"]), len(report["checks"]), path)

    failed = [(r["suite"], c) for r in reports for c in r["checks"] if not c["success"]]
    critical = [describe_failure(s, c) for s, c in failed if c["severity"] == CRITICAL]
    warnings = [describe_failure(s, c) for s, c in failed if c["severity"] != CRITICAL]
    if warnings:
        send_alert(f"{dataset}: {len(warnings)} data quality warning(s)",
                   "\n".join(warnings), level="warning", cfg=cfg)
    if critical:
        body = "\n".join(critical) + f"\n\nFull results: {results_dir}"
        send_alert(f"{dataset}: {len(critical)} blocking data quality failure(s)", body,
                   level="error", cfg=cfg)
        error = ValidationFailedError(
            f"{dataset} failed {len(critical)} blocking check(s): " + "; ".join(critical))
        error.alerted = True  # the detailed alert above was already sent
        raise error
    return {
        "dataset": dataset,
        "suites": [r["suite"] for r in reports],
        "rows": {r["suite"]: r["rows"] for r in reports},
        "checks": sum(len(r["checks"]) for r in reports),
        "warnings": len(warnings),
        "critical_failures": 0,
    }


# --- Open Food Facts snapshot -------------------------------------------------

def off_expectations(cfg: Config) -> list:
    low, high = cfg["validation"]["off_expected_rows"]
    name_mostly = cfg["validation"].get("off_product_name_mostly", 0.9)
    return [
        gxe.ExpectTableRowCountToBeBetween(min_value=low, max_value=high, severity=CRITICAL),
        gxe.ExpectColumnValuesToNotBeNull(column="barcode", severity=CRITICAL),
        gxe.ExpectColumnValuesToBeUnique(column="barcode", severity=CRITICAL),
        gxe.ExpectColumnValuesToMatchRegex(column="barcode", regex=GTIN13, severity=CRITICAL),
        # The snapshot exists to give evidence: every row needs ingredients or allergens
        gxe.ExpectColumnValuesToBeInSet(column="has_evidence", value_set=[True], severity=CRITICAL),
        gxe.ExpectColumnValuesToBeBetween(column="unique_scans_n", min_value=0, severity=WARNING),
        gxe.ExpectColumnValuesToNotBeNull(column="unique_scans_n", severity=WARNING),
        gxe.ExpectColumnValuesToBeBetween(column="completeness", min_value=0, max_value=1,
                                        severity=WARNING),
        gxe.ExpectColumnValuesToNotBeNull(column="product_name", mostly=name_mostly,
                                        severity=WARNING),
    ]


def load_off(path: Path) -> tuple[list[str], pd.DataFrame, pd.DataFrame]:
    """Columns of the snapshot, one row per product (light columns), one row per allergen tag."""
    if not path.is_file():
        raise DataUnavailableError(f"{path} not found; run filter-off first")
    con = duckdb.connect()
    try:
        columns = [r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()]
        if set(OFF_COLUMNS) - set(columns):
            return columns, pd.DataFrame(), pd.DataFrame()
        products = con.execute("""
            SELECT barcode, product_name, unique_scans_n, completeness,
                   coalesce(length(trim(ingredients_text)), 0) > 0
                       OR coalesce(len(allergens_tags), 0) > 0 AS has_evidence
            FROM read_parquet(?)""", [str(path)]).df()
        tags = con.execute(
            "SELECT unnest(allergens_tags) AS tag FROM read_parquet(?)", [str(path)]).df()
    except duckdb.Error as exc:
        raise DataCorruptError(f"cannot read {path}: {exc}; re-run filter-off") from exc
    finally:
        con.close()
    return columns, products, tags


def run_off(cfg: Config, **_: Any) -> dict[str, Any]:
    """Validate data/processed/off_products.parquet (schema, barcodes, evidence, tags)."""
    path = cfg.path(cfg["off"]["output"])
    columns, products, tags = load_off(path)
    context = new_context()

    schema = validate_frame(context, pd.DataFrame(columns=columns), "off_schema", [
        gxe.ExpectTableColumnsToMatchSet(column_set=OFF_COLUMNS, exact_match=False,
                                       severity=CRITICAL),
    ])
    if not schema["success"]:  # the other suites need these columns
        return finish(cfg, "off_products", [schema])

    tag_mostly = cfg["validation"].get("off_tag_format_mostly", 0.99)
    reports = [
        schema,
        validate_frame(context, products, "off_products", off_expectations(cfg)),
        validate_frame(context, tags, "off_allergen_tags", [
            gxe.ExpectColumnValuesToMatchRegex(column="tag", regex=OFF_TAG, mostly=tag_mostly,
                                             severity=CRITICAL),
            gxe.ExpectTableRowCountToBeBetween(min_value=1, severity=WARNING),
        ]),
    ]
    return finish(cfg, "off_products", reports)


# --- Dish catalog ---------------------------------------------------------------

def load_catalog(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One row per dish and one row per (dish, ingredient), read from the raw JSON.

    The JSON is not parsed with the pydantic model first: GE should see (and report)
    exactly what was written, including values the model would reject.
    """
    if not path.is_file():
        raise DataUnavailableError(f"{path} not found; run assemble-catalog first")
    try:
        dishes = json.loads(path.read_text())["dishes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DataCorruptError(f"{path} is not a catalog JSON ({exc}); re-run assemble-catalog"
                               ) from exc

    dish_rows, ingredient_rows = [], []
    for dish in dishes:
        ingredients = dish.get("ingredients") or []
        n = len(ingredients)
        dish_rows.append({
            "dish_id": dish.get("dish_id"),
            "name": dish.get("name"),
            "cuisine": dish.get("cuisine"),
            "recipe_count": dish.get("recipe_count"),
            "n_ingredients": n,
            "n_core": sum(i.get("role") == Role.core.value for i in ingredients),
            "llm_added_share": sum(bool(i.get("added_by_llm")) for i in ingredients) / n if n
            else 0.0,
        })
        ingredient_rows += [{
            "dish_id": dish.get("dish_id"),
            "name": i.get("name"),
            "role": i.get("role"),
            "category": i.get("category"),
            "share": i.get("share"),
        } for i in ingredients]
    dish_columns = ["dish_id", "name", "cuisine", "recipe_count", "n_ingredients", "n_core",
                    "llm_added_share"]
    ingredient_columns = ["dish_id", "name", "role", "category", "share"]
    return (pd.DataFrame(dish_rows, columns=dish_columns),
            pd.DataFrame(ingredient_rows, columns=ingredient_columns))


def run_catalog(cfg: Config, **_: Any) -> dict[str, Any]:
    """Validate data/processed/dish_profiles.json (counts, ids, enums, ingredient rules)."""
    settings = cfg["catalog"]
    dishes, ingredients = load_catalog(cfg.path(settings["profiles_output"]))
    context = new_context()
    reports = [
        validate_frame(context, dishes, "catalog_dishes", [
            gxe.ExpectTableRowCountToBeBetween(min_value=settings["min_dishes"],
                                             max_value=sum(settings["quotas"].values()),
                                             severity=CRITICAL),
            gxe.ExpectColumnValuesToNotBeNull(column="dish_id", severity=CRITICAL),
            gxe.ExpectColumnValuesToBeUnique(column="dish_id", severity=CRITICAL),
            gxe.ExpectColumnValuesToMatchRegex(column="dish_id", regex=SLUG, severity=CRITICAL),
            gxe.ExpectColumnValuesToNotBeNull(column="name", severity=CRITICAL),
            gxe.ExpectColumnValuesToBeUnique(column="name", severity=CRITICAL),
            gxe.ExpectColumnValuesToBeInSet(column="cuisine", value_set=[c.value for c in Cuisine],
                                          severity=CRITICAL),
            gxe.ExpectColumnValuesToNotBeNull(column="cuisine", severity=CRITICAL),
            gxe.ExpectColumnValuesToBeBetween(column="n_ingredients", min_value=3,
                                            severity=CRITICAL),
            gxe.ExpectColumnValuesToBeBetween(column="n_core", min_value=1, severity=CRITICAL),
            gxe.ExpectColumnValuesToBeBetween(column="recipe_count", min_value=0,
                                            severity=CRITICAL),
            gxe.ExpectColumnValuesToBeBetween(column="llm_added_share", min_value=0,
                                            max_value=settings["max_llm_added_share"],
                                            severity=WARNING),
        ]),
        validate_frame(context, ingredients, "catalog_ingredients", [
            gxe.ExpectColumnValuesToNotBeNull(column="name", severity=CRITICAL),
            gxe.ExpectCompoundColumnsToBeUnique(column_list=["dish_id", "name"], severity=CRITICAL),
            gxe.ExpectColumnValuesToBeInSet(column="role", value_set=[r.value for r in Role],
                                          severity=CRITICAL),
            gxe.ExpectColumnValuesToNotBeNull(column="role", severity=CRITICAL),
            gxe.ExpectColumnValuesToBeInSet(column="category",
                                          value_set=[c.value for c in Category],
                                          severity=CRITICAL),
            gxe.ExpectColumnValuesToNotBeNull(column="category", severity=CRITICAL),
            gxe.ExpectColumnValuesToBeBetween(column="share", min_value=0, max_value=1,
                                            severity=CRITICAL),
            gxe.ExpectColumnValuesToNotBeNull(column="share", severity=CRITICAL),
        ]),
    ]
    return finish(cfg, "catalog", reports)
