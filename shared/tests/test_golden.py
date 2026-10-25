"""The hand-written golden recipes must all match the recipe schema."""

import json
from collections import Counter
from pathlib import Path

import pytest

from pantrypath_core.schema import Recipe

GOLDEN = Path(__file__).resolve().parents[2] / "data" / "golden" / "recipes.json"
RECORDS = json.loads(GOLDEN.read_text())


@pytest.mark.parametrize("record", RECORDS, ids=[r.get("dish", "?") for r in RECORDS])
def test_golden_recipe_is_valid(record):
    Recipe.model_validate(record)


def test_golden_dishes_are_unique():
    dishes = Counter(r["dish"].lower() for r in RECORDS)
    assert [d for d, n in dishes.items() if n > 1] == []
