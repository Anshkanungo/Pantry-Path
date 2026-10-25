"""Statistics for the reference data: reports/stats.json (+ outliers.json).

stats.json is a DVC metrics file: nested dicts of plain numbers, so `dvc metrics diff`
can show how the data changed between commits, and drift.py compares it with the
previous run. Lists of example rows go to outliers.json instead.

Outliers use the IQR rule: a value below Q1 - k*IQR or above Q3 + k*IQR is flagged
(k = outliers.iqr_k). They are logged and alerted as warnings, never a failure: a
dish with 45 ingredients is suspicious (often two recipes merged), not necessarily wrong.
Right-skewed counts (recipes per dish, ingredient-statement length) are checked on a
log scale, otherwise every popular dish or long label would be flagged: on the real
OFF snapshot the plain rule flags 6% of products, the log rule 0.02%.
"""

import json
import logging
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from pantrypath_core.catalog import Cuisine, Role
from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError

log = logging.getLogger(__name__)

# Open Food Facts allergen tags per Pantry Path allergen. OFF has no separate wheat tag
# (wheat is folded into en:gluten), so wheat is not reported here.
OFF_ALLERGEN_TAGS = {
    "milk": ["en:milk"],
    "egg": ["en:eggs"],
    "fish": ["en:fish"],
    "shellfish": ["en:crustaceans", "en:molluscs"],
    "tree_nuts": ["en:nuts"],
    "peanuts": ["en:peanuts"],
    "soy": ["en:soybeans"],
    "sesame": ["en:sesame-seeds"],
    "gluten": ["en:gluten"],
}
HAS_ING = "coalesce(length(trim(ingredients_text)), 0) > 0"
HAS_ALLERGENS = "coalesce(len(allergens_tags), 0) > 0"


def iqr_bounds(values: list[float], k: float) -> tuple[float, float]:
    """(low, high) fences of the IQR rule; values outside them are outliers."""
    if len(values) < 4:
        return float("-inf"), float("inf")  # too few values to call anything unusual
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr


def find_outliers(items: dict[str, float], k: float, top: int, *, log_scale: bool = False,
                  upper_only: bool = False) -> dict[str, Any]:
    """IQR outliers among named values: count, fences and the most extreme examples.

    log_scale applies the rule to log(1 + value), for right-skewed counts; upper_only
    ignores small values (a 5-character ingredient statement like "water" is normal).
    Fences are reported in the original units.
    """
    scale = math.log1p if log_scale else float
    unscale = math.expm1 if log_scale else float
    low, high = iqr_bounds([scale(v) for v in items.values()], k)
    if upper_only:
        low = float("-inf")
    flagged = {name: v for name, v in items.items() if not low <= scale(v) <= high}
    median = statistics.median(scale(v) for v in items.values()) if items else 0
    worst = sorted(flagged.items(), key=lambda kv: abs(scale(kv[1]) - median), reverse=True)
    return {
        "count": len(flagged),
        "low": unscale(low) if low != float("-inf") else None,
        "high": unscale(high) if high != float("inf") else None,
        "examples": [{"id": name, "value": value} for name, value in worst[:top]],
    }


def _rate(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


# --- Open Food Facts --------------------------------------------------------------

def _empty_condition(column: str, column_type: str) -> str:
    """SQL that is true when a value is missing: NULL, blank text or an empty list."""
    quoted = f'"{column}"'
    if column_type.endswith("[]"):
        return f"({quoted} IS NULL OR len({quoted}) = 0)"
    if column_type == "VARCHAR":
        return f"({quoted} IS NULL OR trim({quoted}) = '')"
    return f"({quoted} IS NULL)"


def tier_names(bins: list[int]) -> list[tuple[str, int, int | None]]:
    """[0, 1, 10] -> [('0', 0, 1), ('1_9', 1, 10), ('10_plus', 10, None)]"""
    tiers = []
    for i, low in enumerate(bins):
        high = bins[i + 1] if i + 1 < len(bins) else None
        if high is None:
            name = f"{low}_plus"
        elif high == low + 1:
            name = str(low)
        else:
            name = f"{low}_{high - 1}"
        tiers.append((name, low, high))
    return tiers


def text_length_outliers(con: duckdb.DuckDBPyConnection, k: float, top: int) -> dict[str, Any]:
    """Unusually long ingredient statements (log-scale IQR, upper side), inside DuckDB."""
    lengths = ("SELECT barcode, product_name, length(trim(ingredients_text)) AS n "
               f"FROM off WHERE {HAS_ING}")
    q1, q3 = con.execute(f"""
        SELECT quantile_cont(ln(1 + n), 0.25), quantile_cont(ln(1 + n), 0.75)
        FROM ({lengths})""").fetchone()
    if q1 is None:
        return {"count": 0, "low": None, "high": None, "examples": []}
    high = math.expm1(q3 + k * (q3 - q1))
    count = con.execute(f"SELECT count(*) FROM ({lengths}) WHERE n > ?", [high]).fetchone()[0]
    examples = con.execute(f"""
        SELECT barcode, n, product_name FROM ({lengths}) WHERE n > ?
        ORDER BY n DESC, barcode LIMIT ?""", [high, top]).fetchall()
    return {
        "count": count, "low": None, "high": high,
        "examples": [{"id": b, "value": n, "product_name": name} for b, n, name in examples],
    }


def off_stats(path: Path, cfg: Config) -> tuple[dict[str, Any], dict[str, Any]]:
    """Row count, missing-value rates, evidence and allergen coverage, popularity tiers."""
    k, top = cfg["outliers"]["iqr_k"], cfg["outliers"].get("top_offenders", 5)
    bins = cfg.get("bias", {}).get("popularity_bins", [0, 1, 10])
    con = duckdb.connect()
    try:
        escaped = path.as_posix().replace("'", "''")
        con.execute(f"CREATE TEMP VIEW off AS SELECT * FROM read_parquet('{escaped}')")
        types = {row[0]: row[1] for row in con.execute("DESCRIBE off").fetchall()}
        rows = con.execute("SELECT count(*) FROM off").fetchone()[0]

        missing_sql = ", ".join(f"avg({_empty_condition(c, t)}::INT)" for c, t in types.items())
        missing = con.execute(f"SELECT {missing_sql} FROM off").fetchone()

        ing, alg, both, either = con.execute(f"""
            SELECT avg(({HAS_ING})::INT), avg(({HAS_ALLERGENS})::INT),
                   avg(({HAS_ING} AND {HAS_ALLERGENS})::INT),
                   avg(({HAS_ING} OR {HAS_ALLERGENS})::INT)
            FROM off""").fetchone()

        coverage_sql = ", ".join(
            f"avg(coalesce(list_has_any(allergens_tags, {tags}), false)::INT)"
            for tags in OFF_ALLERGEN_TAGS.values())
        coverage = con.execute(f"SELECT {coverage_sql} FROM off").fetchone()

        tier_sql = ", ".join(
            f"avg((coalesce(unique_scans_n, 0) >= {low}"
            + (f" AND coalesce(unique_scans_n, 0) < {high}" if high is not None else "")
            + ")::INT)"
            for _, low, high in tier_names(bins))
        tiers = con.execute(f"SELECT {tier_sql} FROM off").fetchone()

        length_min, length_median, length_p95, length_max = con.execute(f"""
            SELECT min(n), median(n), quantile_disc(n, 0.95), max(n)
            FROM (SELECT length(trim(ingredients_text)) AS n FROM off WHERE {HAS_ING})
            """).fetchone()
        outliers = text_length_outliers(con, k, top)
    except duckdb.Error as exc:
        raise DataCorruptError(f"cannot compute stats on {path}: {exc}") from exc
    finally:
        con.close()

    def rounded(values) -> list[float]:
        return [round(v or 0.0, 6) for v in values]

    stats = {
        "rows": rows,
        "file_mb": round(path.stat().st_size / 1e6, 2),
        "null_rate": dict(zip(types, rounded(missing), strict=True)),
        "evidence_rate": dict(zip(["ingredients", "allergens", "both", "either"],
                                  rounded([ing, alg, both, either]), strict=True)),
        "allergen_coverage": dict(zip(OFF_ALLERGEN_TAGS, rounded(coverage), strict=True)),
        "scans_tier": dict(zip([t[0] for t in tier_names(bins)], rounded(tiers), strict=True)),
        "ingredients_text_length": {
            "min": length_min or 0, "median": length_median or 0,
            "p95": length_p95 or 0, "max": length_max or 0,
        },
    }
    return stats, {"off_ingredients_text_length": outliers}


# --- Dish catalog -------------------------------------------------------------------

def catalog_stats(path: Path, cfg: Config) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dish counts per cuisine, ingredients per dish, core/common split, LLM-added share."""
    k, top = cfg["outliers"]["iqr_k"], cfg["outliers"].get("top_offenders", 5)
    try:
        dishes = json.loads(path.read_text())["dishes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DataCorruptError(f"{path} is not a catalog JSON ({exc})") from exc

    ids = [d.get("dish_id") or f"dish#{n}" for n, d in enumerate(dishes)]
    per_dish = {i: len(d.get("ingredients") or []) for i, d in zip(ids, dishes, strict=True)}
    recipes = {i: d.get("recipe_count") or 0 for i, d in zip(ids, dishes, strict=True)}
    ingredients = [i for d in dishes for i in d.get("ingredients") or []]
    counts = list(per_dish.values()) or [0]
    recipe_values = list(recipes.values()) or [0]
    cuisines = {c.value: 0 for c in Cuisine}
    for d in dishes:
        cuisine = d.get("cuisine") or "missing"
        cuisines[cuisine] = cuisines.get(cuisine, 0) + 1

    stats = {
        "dishes": len(dishes),
        "cuisine": cuisines,
        "ingredients_total": len(ingredients),
        "core": sum(i.get("role") == Role.core.value for i in ingredients),
        "common": sum(i.get("role") == Role.common.value for i in ingredients),
        "llm_added_share": _rate(sum(bool(i.get("added_by_llm")) for i in ingredients),
                                 len(ingredients)),
        "ingredients_per_dish": {
            "min": min(counts), "median": statistics.median(counts), "max": max(counts),
            "mean": round(statistics.fmean(counts), 3),
        },
        "recipe_count": {
            "min": min(recipe_values), "median": statistics.median(recipe_values),
            "max": max(recipe_values),
        },
    }
    outliers = {
        "catalog_ingredients_per_dish": find_outliers(per_dish, k, top),
        "catalog_recipe_count": find_outliers(recipes, k, top, log_scale=True),
    }
    return stats, outliers


def numeric_only(value: Any) -> Any:
    """Keep numbers (and dicts of numbers); drop text and lists, as DVC metrics expect."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict):
        kept = {k: numeric_only(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if v is not None and v != {}}
    return None


# --- Step ----------------------------------------------------------------------------

def fingerprint(paths: list[Path]) -> dict[str, list[int]]:
    """Size and modification time of each input, to recognise a rerun on the same data."""
    return {p.name: [p.stat().st_size, p.stat().st_mtime_ns] for p in paths if p.is_file()}


def run(cfg: Config, **_: Any) -> dict[str, Any]:
    """Compute stats.json and outliers.json from the OFF snapshot and the dish catalog."""
    off_path = cfg.path(cfg["off"]["output"])
    catalog_path = cfg.path(cfg["catalog"]["profiles_output"])
    summary_path = cfg.path(cfg["paths"]["reports"]) / "catalog_summary.json"
    if not off_path.is_file() and not catalog_path.is_file():
        raise DataUnavailableError(
            f"neither {off_path} nor {catalog_path} exists; run filter-off / assemble-catalog")

    stats: dict[str, Any] = {}
    outliers: dict[str, Any] = {}
    if off_path.is_file():
        stats["off"], found = off_stats(off_path, cfg)
        outliers.update(found)
    else:
        log.warning("%s missing: OFF statistics skipped", off_path)
    if catalog_path.is_file():
        stats["catalog"], found = catalog_stats(catalog_path, cfg)
        outliers.update(found)
    else:
        log.warning("%s missing: catalog statistics skipped", catalog_path)
    if summary_path.is_file():
        build = numeric_only(json.loads(summary_path.read_text()))
        if build:
            stats["catalog_build"] = build
    stats["outliers"] = {name: o["count"] for name, o in outliers.items()}

    output = cfg.path(cfg["stats"]["output"])
    previous = cfg.path(cfg["stats"]["previous"])
    meta_path = cfg.path(cfg["stats"]["meta_output"])
    inputs = fingerprint([off_path, catalog_path])
    old_meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the last run as the drift baseline, unless this is a rerun on the same inputs
    # (re-running the task must not overwrite the baseline with identical numbers).
    if output.is_file() and old_meta.get("inputs") != inputs:
        output.replace(previous)
    output.write_text(json.dumps(stats, indent=2) + "\n")
    meta_path.write_text(json.dumps({
        "generated_at": datetime.now(UTC).isoformat(), "inputs": inputs,
        "off": str(off_path), "catalog": str(catalog_path)}, indent=2) + "\n")
    cfg.path(cfg["stats"]["outliers_output"]).write_text(
        json.dumps(outliers, indent=2, default=str) + "\n")
    log.info("stats written to %s", output)

    flagged = {name: count for name, count in stats["outliers"].items() if count}
    if flagged:
        lines = [f"{name}: {count} outlier(s), e.g. "
                 + ", ".join(f"{e['id']}={e['value']}" for e in outliers[name]["examples"][:3])
                 for name, count in flagged.items()]
        send_alert(f"{sum(flagged.values())} outlier(s) found", "\n".join(lines),
                   level="warning", cfg=cfg)
    return {
        "off_rows": stats.get("off", {}).get("rows"),
        "dishes": stats.get("catalog", {}).get("dishes"),
        "outliers": stats["outliers"],
    }
