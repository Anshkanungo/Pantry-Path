"""Tiny datasets in the exact shapes the validation steps read.

off_products.parquet rows (as built by filter-off) and dish_profiles.json (a
pantrypath_core.catalog.Catalog), small enough to reason about in each test.
"""

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

TAGS = pa.list_(pa.string())
OFF_SCHEMA = pa.schema([
    ("barcode", pa.string()), ("code", pa.string()), ("product_name", pa.string()),
    ("brands", pa.string()), ("categories_tags", TAGS), ("allergens_tags", TAGS),
    ("traces_tags", TAGS), ("ingredients_text", pa.string()), ("labels_tags", TAGS),
    ("ingredients_analysis_tags", TAGS), ("unique_scans_n", pa.int64()),
    ("completeness", pa.float64()), ("last_modified_t", pa.int64()),
])

# Small quotas so a 4-dish catalog is complete: 2 american + 1 european + 1 south asian
SMALL_QUOTAS = {"american": 2, "european": 1, "latin_american": 0, "east_se_asian": 0,
                "south_asian": 1, "middle_eastern_african": 0}


def off_row(barcode: str = "0000000000017", **overrides: Any) -> dict[str, Any]:
    row = {
        "barcode": barcode, "code": (barcode or "").lstrip("0"), "product_name": "Whole Milk",
        "brands": "Kroger", "categories_tags": ["en:dairies"], "allergens_tags": ["en:milk"],
        "traces_tags": [], "ingredients_text": "milk, vitamin d3", "labels_tags": [],
        "ingredients_analysis_tags": ["en:vegetarian"], "unique_scans_n": 3,
        "completeness": 0.8, "last_modified_t": 1_700_000_000,
    }
    row.update(overrides)
    return row


def off_rows(n: int) -> list[dict[str, Any]]:
    return [off_row(f"{i + 1:013d}") for i in range(n)]


def write_off(path: Path, rows: list[dict[str, Any]], drop: tuple[str, ...] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=OFF_SCHEMA)
    pq.write_table(table.drop_columns(list(drop)), path)
    return path


def dish(dish_id: str, cuisine: str = "american", n_ingredients: int = 5, llm_added: int = 0,
         recipe_count: int = 50, **overrides: Any) -> dict[str, Any]:
    ingredients = [{
        "name": f"ingredient {k}", "role": "core" if k == 0 else "common", "category": "other",
        "share": 0.0 if k < llm_added else 0.5, "added_by_llm": k < llm_added,
    } for k in range(n_ingredients)]
    data = {
        "dish_id": dish_id, "name": dish_id.replace("-", " "), "aliases": [],
        "cuisine": cuisine, "ingredients": ingredients, "recipe_count": recipe_count,
        "review_count": 10, "source": "foodcom", "catalog_version": "test",
    }
    data.update(overrides)
    return data


def small_catalog() -> list[dict[str, Any]]:
    return [dish("lasagna", "american"), dish("meatloaf", "american"),
            dish("quiche-lorraine", "european"), dish("chicken-tikka-masala", "south_asian")]


def write_catalog(path: Path, dishes: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "catalog_version": "test",
        "generated_at": "2026-10-24T00:00:00Z", "dishes": dishes,
    }))
    return path


def use_small_limits(cfg) -> None:
    """Shrink row-count and dish-count limits so the tiny datasets fit."""
    cfg.data["validation"]["off_expected_rows"] = [3, 1000]
    cfg.data["catalog"]["quotas"] = dict(SMALL_QUOTAS)
    cfg.data["catalog"]["min_dishes"] = 3
