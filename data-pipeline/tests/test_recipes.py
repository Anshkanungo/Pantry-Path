"""Recipe cleaning: titles, ingredient names, missing values, the clean-recipes step."""

import math

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from pantrypath_pipeline.acquire.foodcom import raw_path
from pantrypath_pipeline.errors import DataUnavailableError
from pantrypath_pipeline.preprocess import recipes
from pantrypath_pipeline.preprocess.recipes import (
    clean_frame,
    clean_ingredient,
    clean_ingredients,
    clean_keywords,
    clean_title,
    fix_text,
    singularize,
)


@pytest.mark.parametrize("raw, expected", [
    ("eggs", "egg"),
    ("Large Eggs", "egg"),
    ("8-inch flour tortillas", "flour tortilla"),
    ("2% low-fat milk", "low-fat milk"),
    ("1% fat cottage cheese", "cottage cheese"),
    ("½ cup milk", "milk"),
    ("1 1/2 cups heavy cream", "heavy cream"),
    ("2 (15 ounce) cans diced tomatoes", "diced tomato"),
    ("chopped tomatoes", "tomato"),
    ("diced tomatoes", "diced tomato"),  # canned diced tomatoes are a product
    ("fresh ground black pepper", "ground black pepper"),
    ("Lea &amp; Perrins Worcestershire Sauce", "lea & perrins worcestershire sauce"),
    ("Gebhardt&reg; Chili powder", "gebhardt chili powder"),
    ("Tony’s Pizza Dough", "tony's pizza dough"),
    ("French Dressing (Not Catalina Style)", "french dressing"),
    ("lemons, zest of", "lemon zest"),
    ("lemon, rind of", "lemon zest"),
    ("lime, juice of", "lime juice"),
    ("limes, juice and zest of", "lime"),
    ("7-up", "7-up"),
    ("red pepper flakes", "red pepper flakes"),
    ("molasses", "molasses"),
])
def test_clean_ingredient(raw, expected):
    assert clean_ingredient(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "12", "water", "Boiling Water", "ice cubes"])
def test_clean_ingredient_drops_empty_and_non_groceries(raw):
    assert clean_ingredient(raw) is None


@pytest.mark.parametrize("word, singular", [
    ("tomatoes", "tomato"), ("joes", "joe"), ("berries", "berry"), ("cookies", "cookie"),
    ("peaches", "peach"), ("leaves", "leaf"), ("quiches", "quiche"), ("glasses", "glass"),
    ("hummus", "hummus"), ("couscous", "couscous"), ("oats", "oats"), ("egg", "egg"),
])
def test_singularize(word, singular):
    assert singularize(word) == singular


def test_clean_ingredients_dedupes_and_handles_missing_values():
    assert clean_ingredients(np.array(["walnuts", "walnuts", "Water", "eggs", "egg", None],
                                      dtype=object)) == ["walnut", "egg"]
    assert clean_ingredients(None) == []
    assert clean_ingredients(float("nan")) == []
    assert clean_ingredients([]) == []


@pytest.mark.parametrize("raw, expected", [
    ("&quot;Best&quot; Brownies!", 'best" brownies'),
    ("Rote Gr&uuml;tze", "rote grütze"),
    ("M&amp;amp;m Bars", "m&m bars"),  # double-escaped
    ("  Pancakes  ", "pancakes"),
    ("!!!", None),
    (None, None),
    (float("nan"), None),
])
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


def test_fix_text_handles_pandas_missing_values():
    assert fix_text(pd.NA) == "" and fix_text(None) == "" and fix_text(math.nan) == ""


def test_clean_keywords():
    assert clean_keywords(np.array(["Easy", "easy", "Southwest Asia (middle East)"],
                                   dtype=object)) == ["easy", "southwest asia (middle east)"]
    assert clean_keywords(None) == []


def raw_frame():
    return pd.DataFrame({
        "RecipeId": [38.0, 39.0, 39.0, 40.0, 41.0, 42.0],
        "Name": ["Quiche Lorraine", "Pad Thai", "Pad Thai (dupe)", None, "Ice Water", "Chili"],
        "RecipeIngredientParts": [
            np.array(["eggs", "heavy cream", "bacon"], dtype=object),
            np.array(["rice noodles", "eggs"], dtype=object),
            np.array(["rice noodles"], dtype=object),
            np.array(["salt"], dtype=object),
            np.array(["water", "ice cubes"], dtype=object),
            None,
        ],
        "Keywords": [np.array(["European"], dtype=object), np.array(["Thai"], dtype=object),
                     None, None, None, np.array([], dtype=object)],
        "RecipeCategory": ["Breakfast", "Asian", "Asian", None, "Beverages", "Meat"],
        "ReviewCount": [12.0, None, 1.0, 3.0, 2.0, 1.0],
        "AggregatedRating": [4.5, None, 3.0, 5.0, 4.0, 4.0],
    })


def test_clean_frame_drops_and_counts_unusable_rows():
    clean, dropped = clean_frame(raw_frame())
    assert dropped == {"duplicate_id": 1, "no_title": 1, "no_ingredients": 2}
    assert list(clean["recipe_id"]) == [38, 39]
    assert list(clean["ingredients"][0]) == ["egg", "heavy cream", "bacon"]
    assert list(clean["review_count"]) == [12, 0]  # missing review counts become 0
    assert clean["keywords"][1] == ["thai"]


def test_run_writes_the_clean_parquet(cfg):
    source = raw_path(cfg)
    source.parent.mkdir(parents=True, exist_ok=True)
    raw_frame().to_parquet(source)
    summary = recipes.run(cfg)
    assert summary["recipes_out"] == 2 and summary["recipes_in"] == 6
    table = pq.read_table(cfg.path(cfg["foodcom"]["clean_output"]))
    assert table.schema == recipes.OUTPUT_SCHEMA
    assert table.column("title").to_pylist() == ["quiche lorraine", "pad thai"]


def test_run_without_the_raw_file_says_what_to_do(cfg):
    with pytest.raises(DataUnavailableError, match="download-foodcom"):
        recipes.run(cfg)
