"""SQLite catalog: atomic build and dish lookup (exact, alias, fuzzy, miss)."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from pantrypath_core.catalog import Catalog
from pantrypath_pipeline import store
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError


def dish(dish_id, name, aliases=(), reviews=100, cuisine="american"):
    return {"dish_id": dish_id, "name": name, "aliases": list(aliases), "cuisine": cuisine,
            "recipe_count": 10, "review_count": reviews, "source": "foodcom",
            "catalog_version": "2026-10-24",
            "ingredients": [
                {"name": "eggs", "role": "core", "category": "egg", "share": 0.9},
                {"name": "pie crust", "role": "core", "category": "bakery", "share": 0,
                 "added_by_llm": True},
                {"name": "onion", "role": "common", "category": "produce", "share": 0.3}]}


@pytest.fixture
def db(cfg):
    catalog = Catalog(catalog_version="2026-10-24", generated_at=datetime.now(UTC), dishes=[
        dish("quiche-lorraine", "quiche lorraine", ["bacon quiche"], cuisine="european"),
        dish("chicken-pot-pie", "chicken pot pie", reviews=900),
        dish("lasagna", "lasagna", ["lasagne"], reviews=5000, cuisine="european"),
    ])
    cfg.path(cfg["catalog"]["profiles_output"]).write_text(catalog.model_dump_json())
    summary = store.run(cfg)
    assert summary == {"catalog_version": "2026-10-24", "dishes": 3, "aliases": 5,
                       "ingredients": 9}
    return cfg.path(cfg["catalog"]["sqlite_output"])


def test_build_writes_all_tables_atomically(db):
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT count(*) FROM dish_ingredients").fetchone()[0] == 9
        meta = dict(con.execute("SELECT key, value FROM meta"))
    assert meta["catalog_version"] == "2026-10-24" and "built_at" in meta
    assert not list(db.parent.glob(".*.tmp"))


@pytest.mark.parametrize("query", ["Quiche Lorraine", "  bacon   QUICHE ", "quiche lorain"])
def test_find_dish_exact_alias_and_typo(db, query):
    profile = store.find_dish(db, query, 90)
    assert profile.dish_id == "quiche-lorraine"
    assert profile.aliases == ["bacon quiche"]
    assert [i.name for i in profile.ingredients] == ["eggs", "pie crust", "onion"]
    assert profile.ingredients[1].added_by_llm is True


@pytest.mark.parametrize("query", ["chicken", "pizza", "", "pie"])
def test_find_dish_misses_rather_than_guessing(db, query):
    assert store.find_dish(db, query, 90) is None


def test_find_dish_without_catalog(tmp_path):
    with pytest.raises(DataUnavailableError, match="load-sqlite"):
        store.find_dish(tmp_path / "missing.sqlite", "lasagna", 90)


def test_run_rejects_a_broken_profiles_file(cfg):
    cfg.path(cfg["catalog"]["profiles_output"]).write_text(json.dumps({"dishes": "nope"}))
    with pytest.raises(DataCorruptError):
        store.run(cfg)
    cfg.path(cfg["catalog"]["profiles_output"]).unlink()
    with pytest.raises(DataUnavailableError, match="assemble-catalog"):
        store.run(cfg)
