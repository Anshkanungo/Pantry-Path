"""SQLite dish catalog: built from dish_profiles.json, read by the request flow.

The database is rebuilt from scratch each run into a temp file and then renamed over the
old one, so a reader never sees a half-built catalog.

find_dish() matches a user's dish text: an exact name or alias first, then fuzzy
matching with rapidfuzz token_sort_ratio. That scorer ignores word order and tolerates
typos ("quiche lorain"), but unlike token_set_ratio or WRatio it does not give a short
query a near-perfect score for being part of a longer name ("chicken" vs "chicken pot
pie" scores 64, not 90-100), so generic words don't match a specific dish.
"""

import json
import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rapidfuzz import fuzz, utils

from pantrypath_core.catalog import Catalog, DishProfile, normalize_name
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE dishes (
    dish_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    cuisine TEXT NOT NULL,
    recipe_count INTEGER NOT NULL,
    review_count INTEGER NOT NULL,
    source TEXT NOT NULL,
    catalog_version TEXT NOT NULL
);
CREATE TABLE dish_aliases (
    alias TEXT NOT NULL,
    dish_id TEXT NOT NULL REFERENCES dishes (dish_id),
    PRIMARY KEY (alias, dish_id)
);
CREATE TABLE dish_ingredients (
    dish_id TEXT NOT NULL REFERENCES dishes (dish_id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('core', 'common')),
    category TEXT NOT NULL,
    share REAL NOT NULL,
    added_by_llm INTEGER NOT NULL,
    PRIMARY KEY (dish_id, position)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX dish_aliases_by_alias ON dish_aliases (alias);
"""


def build_sqlite(catalog: Catalog, db_path: Path) -> dict[str, int]:
    """Write the catalog to db_path atomically; returns row counts."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=db_path.parent, prefix=f".{db_path.name}.", suffix=".tmp")
    os.close(fd)
    counts = {"dishes": 0, "aliases": 0, "ingredients": 0}
    try:
        with closing(sqlite3.connect(tmp)) as con:
            con.executescript(SCHEMA)
            for dish in catalog.dishes:
                con.execute(
                    "INSERT INTO dishes VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (dish.dish_id, dish.name, dish.cuisine.value, dish.recipe_count,
                     dish.review_count, dish.source, dish.catalog_version),
                )
                names = [dish.name, *dish.aliases]  # the name itself is findable as an alias
                con.executemany("INSERT OR IGNORE INTO dish_aliases VALUES (?, ?)",
                                [(n, dish.dish_id) for n in names])
                con.executemany(
                    "INSERT INTO dish_ingredients VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(dish.dish_id, pos, i.name, i.role.value, i.category.value, i.share,
                      int(i.added_by_llm)) for pos, i in enumerate(dish.ingredients)],
                )
                counts["dishes"] += 1
                counts["aliases"] += len(names)
                counts["ingredients"] += len(dish.ingredients)
            con.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("catalog_version", catalog.catalog_version),
                ("schema_version", catalog.schema_version),
                ("built_at", datetime.now(UTC).isoformat(timespec="seconds")),
            ])
            con.commit()
        os.replace(tmp, db_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return counts


def run(cfg: Config) -> dict[str, Any]:
    settings = cfg["catalog"]
    source = cfg.path(settings["profiles_output"])
    if not source.is_file():
        raise DataUnavailableError(f"{source} not found; run assemble-catalog first")
    try:
        catalog = Catalog.model_validate(json.loads(source.read_text()))
    except (ValueError, ValidationError) as exc:
        raise DataCorruptError(f"{source} is not a valid catalog: {exc}") from exc
    db_path = cfg.path(settings["sqlite_output"])
    counts = build_sqlite(catalog, db_path)
    log.info("SQLite catalog %s: %s", db_path, counts)
    return {"catalog_version": catalog.catalog_version, **counts}


def _load_profile(con: sqlite3.Connection, dish_id: str) -> DishProfile:
    row = con.execute("SELECT * FROM dishes WHERE dish_id = ?", (dish_id,)).fetchone()
    aliases = [r["alias"] for r in con.execute(
        "SELECT alias FROM dish_aliases WHERE dish_id = ? AND alias != ? ORDER BY alias",
        (dish_id, row["name"]))]
    ingredients = [
        {"name": r["name"], "role": r["role"], "category": r["category"], "share": r["share"],
         "added_by_llm": bool(r["added_by_llm"])}
        for r in con.execute(
            "SELECT * FROM dish_ingredients WHERE dish_id = ? ORDER BY position", (dish_id,))
    ]
    return DishProfile(
        dish_id=row["dish_id"], name=row["name"], aliases=aliases, cuisine=row["cuisine"],
        ingredients=ingredients, recipe_count=row["recipe_count"],
        review_count=row["review_count"], source=row["source"],
        catalog_version=row["catalog_version"],
    )


def find_dish(db_path: str | Path, query: str, min_score: float = 90) -> DishProfile | None:
    """The catalog dish a user's text refers to, or None when nothing is close enough.

    Exact name/alias match first; otherwise the best token_sort_ratio score over names and
    aliases, if >= min_score. Ties go to the more popular dish.
    """
    path = Path(db_path)
    if not path.is_file():
        raise DataUnavailableError(
            f"Dish catalog not found at {path}; run the batch pipeline (load-sqlite) or dvc pull"
        )
    try:
        wanted = normalize_name(query)
    except ValueError:
        return None
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT a.alias, d.dish_id, d.review_count, d.recipe_count "
            "FROM dish_aliases a JOIN dishes d USING (dish_id)"
        ).fetchall()
        exact = [r for r in rows if r["alias"] == wanted]
        if exact:
            best = max(exact, key=lambda r: (r["review_count"], r["recipe_count"]))
            return _load_profile(con, best["dish_id"])
        scored = [
            (fuzz.token_sort_ratio(wanted, r["alias"], processor=utils.default_process),
             r["review_count"], r["recipe_count"], r["dish_id"])
            for r in rows
        ]
        if not scored:
            return None
        score, _, _, dish_id = max(scored)
        if score < min_score:
            log.info("No catalog dish matches %r (best score %.0f < %.0f)", query, score,
                     min_score)
            return None
        log.info("Matched %r to catalog dish %s (score %.0f)", query, dish_id, score)
        return _load_profile(con, dish_id)
