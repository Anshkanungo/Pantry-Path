"""Look up Open Food Facts evidence by barcode in the processed snapshot.

Used by the request flow (when a store has no allergen data for a product) and by
the bias report. Inputs are normalized with to_gtin13, so pass real barcodes; Kroger's
13-digit codes have no check digit and must go through kroger_upc_to_gtin13 first.
"""

import logging
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import duckdb

from pantrypath_core.barcodes import to_gtin13
from pantrypath_core.diet_rules import ProductEvidence
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataUnavailableError

log = logging.getLogger(__name__)


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


def _tags(value: Any) -> list[str]:
    # Rows from pandas hold numpy arrays (or NaN for missing), rows from DuckDB hold lists
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    return [str(v) for v in value]


def evidence_from_row(row: Mapping[str, Any]) -> ProductEvidence:
    """One snapshot row (DuckDB or pandas) -> the evidence the diet rules read."""
    text = _text(row.get("ingredients_text"))
    # The rules read English. A French-only statement would look allergen-free, so it is
    # dropped and the product falls back to declared allergens (or "unknown").
    if text and "ingredients_lang" in row and row.get("ingredients_lang") != "en":
        text = ""
    return ProductEvidence(
        source="off",
        name=_text(row.get("product_name")),
        brand=_text(row.get("brands")),
        categories=_tags(row.get("categories_tags")),
        ingredients_text=text,
        allergens=_tags(row.get("allergens_tags")),
        traces=_tags(row.get("traces_tags")),
    )


def lookup(
    cfg: Config, barcodes: Iterable[str], snapshot: Path | None = None
) -> dict[str, ProductEvidence]:
    """Evidence for every barcode found in the snapshot, keyed by GTIN-13."""
    wanted = sorted({g for g in (to_gtin13(b) for b in barcodes) if g})
    if not wanted:
        return {}
    path = snapshot or cfg.path(cfg["off"]["output"])
    if not path.exists():
        raise DataUnavailableError(
            f"Open Food Facts snapshot {path} not found: run download-off and filter-off, "
            "or `dvc pull` it"
        )
    with duckdb.connect() as con:
        cursor = con.execute(
            "SELECT * FROM read_parquet(?) WHERE list_contains(?, barcode)", [str(path), wanted]
        )
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
    log.debug("OFF lookup: %d of %d barcodes found", len(rows), len(wanted))
    return {row["barcode"]: evidence_from_row(row) for row in rows}
