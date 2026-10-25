"""Catalog assembly: quotas, shortfalls, merges, always_include dishes, min_dishes."""

import json
import shutil

import pytest
from test_refine import FakeLLM

from pantrypath_core.catalog import Catalog
from pantrypath_pipeline import catalog_build, refine
from pantrypath_pipeline.catalog_build import assemble
from pantrypath_pipeline.errors import ValidationFailedError

QUOTAS = {"american": 1, "european": 2, "latin_american": 1, "east_se_asian": 1,
          "south_asian": 1, "middle_eastern_african": 0}


@pytest.fixture
def built(cfg, fixtures_dir, monkeypatch):
    """The sample pool refined by a fake LLM, small quotas, and a captured alert list."""
    shutil.copy(fixtures_dir / "llm_pool_sample.json", cfg.path(cfg["foodcom"]["pool_output"]))
    replies = json.loads((fixtures_dir / "llm_replies_sample.json").read_text())
    refine.run_all(cfg, client=FakeLLM(replies))
    cfg.data["catalog"].update(quotas=QUOTAS, min_dishes=3)
    alerts = []
    monkeypatch.setattr(catalog_build, "send_alert", lambda *a, **kw: alerts.append(a))
    return cfg, alerts


def test_catalog_respects_quotas_and_reports_shortfalls(built):
    cfg, alerts = built
    summary = catalog_build.run(cfg)
    catalog = Catalog.model_validate_json(cfg.path(cfg["catalog"]["profiles_output"]).read_text())
    by_id = {d.dish_id: d for d in catalog.dishes}

    assert sorted(by_id) == ["beef-tacos", "chicken-tikka-masala", "lasagna", "quiche-lorraine"]
    assert summary["shortfalls"] == {"american": 1, "east_se_asian": 1}
    assert alerts and "american" in alerts[0][1] and "east_se_asian" in alerts[0][1]
    # classic-lasagna was merged in: its name is an alias and its counts were added
    assert "classic lasagna" in by_id["lasagna"].aliases
    assert by_id["lasagna"].review_count == 5300
    assert {d.catalog_version for d in catalog.dishes} == {summary["catalog_version"]}

    report = json.loads(cfg.path(cfg["catalog"]["summary_report"]).read_text())
    assert report["dropped"] == ["margarita"]
    assert report["merged"] == [{"dish_id": "classic-lasagna", "into": "lasagna", "reason": "llm"}]
    assert report["per_cuisine"]["european"] == {"quota": 2, "available": 2, "selected": 2,
                                                 "always_include": 0}
    assert json.loads(cfg.path(cfg["llm"]["usage_report"]).read_text())["totals"][
        "calls_real"] == 6


def test_too_few_dishes_fails_and_keeps_the_old_catalog(built):
    cfg, _ = built
    cfg.data["catalog"]["min_dishes"] = 10
    with pytest.raises(ValidationFailedError, match="below catalog.min_dishes=10"):
        catalog_build.run(cfg)
    assert not cfg.path(cfg["catalog"]["profiles_output"]).exists()
    assert cfg.path(cfg["catalog"]["summary_report"]).exists()  # still there to inspect


def test_always_include_ranks_first_and_llm_changes_are_reported(cfg, fixtures_dir, monkeypatch,
                                                                 caplog):
    pool = json.loads((fixtures_dir / "llm_pool_sample.json").read_text())
    flags = {"quiche-lorraine": True, "margarita": True, "classic-lasagna": True}
    for dish in pool["dishes"]:
        dish["always_include"] = flags.get(dish["dish_id"], False)
    cfg.path(cfg["foodcom"]["pool_output"]).write_text(json.dumps(pool))
    replies = json.loads((fixtures_dir / "llm_replies_sample.json").read_text())
    refine.run_all(cfg, client=FakeLLM(replies))
    records = catalog_build.load_refined(cfg, pool["dishes"])

    # European quota of 1: the less popular golden quiche beats lasagna, but classic-lasagna
    # (golden) was merged into lasagna, which inherits the flag and so both are kept
    asm = assemble(pool["dishes"], records, {**QUOTAS, "european": 1})
    european = [d.dish_id for d in asm.selected if d.cuisine.value == "european"]
    assert european == ["lasagna", "quiche-lorraine"]
    assert asm.per_cuisine["european"]["always_include"] == 2
    assert any("exceed the quota" in n for n in asm.notes)

    # Without the merge flag, quiche (900 reviews) outranks lasagna (5300) for one slot
    pool["dishes"][1]["always_include"] = False
    asm = assemble(pool["dishes"], records, {**QUOTAS, "european": 1})
    assert [d.dish_id for d in asm.selected if d.cuisine.value == "european"] == [
        "quiche-lorraine"]

    outcomes = {c["dish_id"]: c["outcome"] for c in asm.always_include_changed}
    assert outcomes == {"margarita": "dropped by the LLM (keep=false)"}
    assert "always_include dish margarita" in caplog.text


def test_merge_chains_and_missing_targets():
    reviews = {"c": 30, "b": 20, "a": 10, "x": 5, "dup": 4}

    def rec(dish_id, merge_into=None, name=None):
        profile = {"dish_id": dish_id, "name": name or dish_id, "cuisine": "american",
                   "recipe_count": 1, "review_count": reviews[dish_id],
                   "source": "foodcom", "catalog_version": "t",
                   "ingredients": [{"name": n, "role": "core", "category": "other", "share": 1}
                                   for n in ("a", "b", "c")]}
        return {"keep": True, "merge_into": merge_into, "name": profile["name"], "aliases": [],
                "profile": profile, "error": None}

    pool = [{"dish_id": i, "name": i, "review_count": r, "recipe_count": 1}
            for i, r in reviews.items()]
    records = {"c": rec("c"), "b": rec("b", "c"), "a": rec("a", "b"),
               "x": rec("x", "gone"), "dup": rec("dup", name="c")}
    asm = assemble(pool, records, {"american": 5})
    ids = sorted(d.dish_id for d in asm.selected)
    assert ids == ["c", "x"]  # a -> b -> c; x's target is missing so it stays; dup = c by name
    target = next(d for d in asm.selected if d.dish_id == "c")
    assert target.review_count == 30 + 20 + 10 + 4
    assert {m["dish_id"]: m["into"] for m in asm.merged} == {"b": "c", "a": "c", "dup": "c"}


def test_aliases_are_unique_across_dishes():
    def rec(dish_id, aliases):
        profile = {"dish_id": dish_id, "name": dish_id, "aliases": aliases, "cuisine": "american",
                   "recipe_count": 1, "review_count": {"chili": 9, "stew": 1}[dish_id],
                   "source": "foodcom", "catalog_version": "t",
                   "ingredients": [{"name": n, "role": "core", "category": "other", "share": 1}
                                   for n in ("a", "b", "c")]}
        return {"keep": True, "merge_into": None, "profile": profile, "error": None}

    pool = [{"dish_id": "chili", "name": "chili"}, {"dish_id": "stew", "name": "stew"}]
    records = {"chili": rec("chili", ["bowl of red", "stew"]), "stew": rec("stew", ["bowl of red"])}
    asm = assemble(pool, records, {"american": 2})
    aliases = {d.dish_id: d.aliases for d in asm.selected}
    assert aliases == {"chili": ["bowl of red"], "stew": []}
