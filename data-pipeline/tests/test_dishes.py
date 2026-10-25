"""Dish grouping and pool selection: names, cuisine hints, shares, the pool JSON."""

import json

import numpy as np
import pandas as pd
import pytest

from pantrypath_pipeline.errors import DataUnavailableError
from pantrypath_pipeline.preprocess import dishes
from pantrypath_pipeline.preprocess.dishes import (
    cuisine_hint,
    dish_key,
    display_name,
    group_dishes,
    ingredient_shares,
    is_non_dish,
    is_non_dish_key,
    recipe_cuisine,
    select_pool,
)


@pytest.mark.parametrize("title, name, key", [
    ("mom's easy chicken pot pie (copycat)", "chicken pot pie", "chicken pot pie"),
    ("kittencal's italian melt-in-your-mouth meatballs", "italian meatballs", "italian meatball"),
    ("to die for crock pot roast", "pot roast", "pot roast"),
    ("crock-pot chicken with black beans & cream cheese",
     "chicken with black beans and cream cheese", "chicken with black beans and cream cheese"),
    ("jo mama's world famous spaghetti", "spaghetti", "spaghetti"),
    ("japanese mum's chicken", "japanese chicken", "japanese chicken"),
    ("the best brownies", "brownies", "brownie"),
    ("banana bread ii", "banana bread", "banana bread"),
    ("lasagna recipe", "lasagna", "lasagna"),
    ("5-minute fudge", "fudge", "fudge"),
    ("shepherd's pie", "shepherd's pie", "shepherds pie"),
    ("easy shepherds pies", "shepherds pies", "shepherds pie"),
    ("sloppy joe's", "sloppy joe's", "sloppy joe"),
    ("mac 'n' cheese", "macaroni and cheese", "macaroni and cheese"),
    ("olive garden copycat zuppa toscana", "zuppa toscana", "zuppa toscana"),
    ("crème brûlée", "crème brûlée", "creme brulee"),
    ("valentine's day cookies", "valentine's day cookies", "valentines day cookie"),
])
def test_display_name_and_key(title, name, key):
    assert display_name(title) == name
    assert dish_key(name) == key


@pytest.mark.parametrize("title", ["easy", "the best", "(copycat)", "", None, float("nan")])
def test_display_name_of_pure_filler_is_none(title):
    assert display_name(title) is None
    assert dish_key(display_name(title)) is None


@pytest.mark.parametrize("keywords, category, expected", [
    (["easy", "mexican"], "one dish meal", "latin_american"),
    (["asian", "indian"], None, "south_asian"),  # specific beats generic
    (["asian"], None, "east_se_asian"),
    ([], "european", "european"),
    (["southwest asia (middle east)"], None, "middle_eastern_african"),
    (["cajun", "chinese", "chinese"], None, "east_se_asian"),
    (["easy", "< 60 mins"], "dessert", None),
    (None, None, None),
    (np.array(["Thai"], dtype=object), float("nan"), "east_se_asian"),
])
def test_recipe_cuisine(keywords, category, expected):
    assert recipe_cuisine(keywords, category) == expected


def test_cuisine_hint_needs_a_minimum_share():
    assert cuisine_hint(["european", None, None, None], 0.2) == ("european", 0.25)
    assert cuisine_hint(["european"] + [None] * 9, 0.2) == ("unknown", 0.1)
    assert cuisine_hint([None, float("nan")], 0.2) == ("unknown", 0.0)
    assert cuisine_hint([], 0.2) == ("unknown", 0.0)
    # a tie goes to the cuisine listed first (stable output)
    assert cuisine_hint(["south_asian", "american"], 0.2)[0] == "american"


def test_non_dish_rules():
    assert is_non_dish("Beverages", [])
    assert is_non_dish(None, np.array(["Household Cleaner"], dtype=object))
    assert not is_non_dish("Pie", ["dessert"])
    assert not is_non_dish(float("nan"), None)
    for key in ["taco seasoning", "cream cheese frosting", "pie crust", "pizza dough",
                "chicken stock", "dog biscuit", "play dough"]:
        assert is_non_dish_key(key), key
    for key in ["carrot cake with cream cheese frosting", "fried rice", "sourdough bread",
                "glazed ham", "crazy crust pizza"]:
        assert not is_non_dish_key(key), key


def test_ingredient_shares_counts_each_recipe_once_and_ranks():
    lists = [["egg", "milk", "egg"], ["egg", "flour"], ["egg", "milk"], ["butter"]]
    assert ingredient_shares(lists, top_n=3) == [
        {"name": "egg", "share": 0.75}, {"name": "milk", "share": 0.5},
        {"name": "butter", "share": 0.25},
    ]
    assert ingredient_shares([], top_n=3) == []


def recipe(rid, title, ingredients, keywords=(), reviews=1, category="One Dish Meal"):
    return {"recipe_id": rid, "title": title, "ingredients": list(ingredients),
            "keywords": list(keywords), "category": category, "review_count": reviews,
            "rating": 4.0}


def sample_recipes():
    rows = [
        recipe(1, "best lasagna", ["lasagna noodle", "ricotta", "beef"], ["european"], 50),
        recipe(2, "lasagna", ["ricotta", "beef", "mozzarella cheese"], [], 10),
        recipe(3, "easy lasagna recipe", ["lasagna noodle", "ricotta"], ["european"], 5),
        recipe(4, "pad thai", ["rice noodle", "egg"], ["thai", "asian"], 20),
        recipe(5, "chicken pad thai", ["rice noodle"], ["thai"], 1),
        recipe(6, "pad thai", ["rice noodle", "peanut"], ["thai"], 2),
        recipe(7, "sangria", ["wine"], [], 99, category="Beverages"),
        recipe(8, "sangria", ["wine"], [], 99, category="Beverages"),
        recipe(9, "taco seasoning", ["chili powder"], [], 70),
        recipe(10, "taco seasoning", ["chili powder"], [], 70),
        recipe(11, "meatloaf", ["beef", "egg"], [], 30),
        recipe(12, "mom's meatloaf", ["beef", "ketchup"], [], 3),
    ]
    return pd.DataFrame(rows)


def test_group_dishes():
    candidates, stats = group_dishes(sample_recipes(), min_recipes=2, top_n=2,
                                     hint_min_share=0.2)
    assert stats["non_dish_recipes"] == 2 and stats["basic_or_component_recipes"] == 2
    assert list(candidates["dish_id"]) == ["lasagna", "meatloaf", "pad-thai"]  # by reviews
    lasagna = candidates.iloc[0]
    assert lasagna["recipe_count"] == 3 and lasagna["review_count"] == 65
    assert lasagna["cuisine_hint"] == "european" and lasagna["hint_share"] == 0.667
    assert lasagna["ingredients"] == [{"name": "ricotta", "share": 1.0},
                                      {"name": "beef", "share": 0.667}]
    assert lasagna["sample_titles"] == ["best lasagna", "lasagna", "easy lasagna recipe"]
    assert candidates.iloc[1]["cuisine_hint"] == "unknown"
    assert candidates.iloc[2]["name"] == "pad thai"  # chicken pad thai is its own key


def test_select_pool_takes_top_n_per_hint(caplog):
    candidates, _ = group_dishes(sample_recipes(), min_recipes=2, top_n=5, hint_min_share=0.2)
    pool = select_pool(candidates, {"european": 1, "east_se_asian": 3, "unknown": 1})
    assert list(pool["dish_id"]) == ["lasagna", "pad-thai", "meatloaf"]
    assert "only 1 candidate dishes with cuisine hint east_se_asian" in caplog.text


def test_select_pool_adds_always_include_dishes_below_the_cut(caplog):
    candidates, _ = group_dishes(sample_recipes(), min_recipes=2, top_n=5, hint_min_share=0.2)
    pool = select_pool(candidates, {"european": 1, "unknown": 0},
                       always_include=["pad-thai", "lasagna", "no-such-dish"])
    assert list(pool["dish_id"]) == ["lasagna", "pad-thai"]
    assert list(pool["always_include"]) == [True, True]
    assert "no-such-dish" in caplog.text


def write_clean(cfg, df):
    path = cfg.path(cfg["foodcom"]["clean_output"])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)


def test_run_group_and_select_write_the_pool_json(cfg):
    cfg["foodcom"]["min_recipes_per_dish"] = 2
    cfg["foodcom"]["pool_size"] = {"european": 5, "east_se_asian": 5, "unknown": 5}
    write_clean(cfg, sample_recipes())
    group_summary = dishes.run_group(cfg)
    assert group_summary["dishes"] == 3
    assert group_summary["hint_mix"]["european"] == 1

    summary = dishes.run_select(cfg)
    assert summary["pool"] == 3
    payload = json.loads(cfg.path(cfg["foodcom"]["pool_output"]).read_text())
    assert set(payload) == {"generated_at", "source", "plain_top100_cuisine_hints", "dishes"}
    assert payload["source"] == "foodcom"
    assert sum(payload["plain_top100_cuisine_hints"].values()) == 3
    entry = payload["dishes"][0]
    assert set(entry) == {"dish_id", "name", "cuisine_hint", "recipe_count", "review_count",
                          "always_include", "sample_titles", "ingredients"}
    assert entry["ingredients"][0] == {"name": "ricotta", "share": 1.0}


def test_run_group_without_clean_recipes_says_what_to_do(cfg):
    with pytest.raises(DataUnavailableError, match="clean-recipes"):
        dishes.run_group(cfg)


def test_run_select_without_candidates_says_what_to_do(cfg):
    with pytest.raises(DataUnavailableError, match="group-dishes"):
        dishes.run_select(cfg)
