"""Catalog and result.json models: valid examples pass, broken ones fail loudly."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from pantrypath_core.catalog import Catalog, CatalogIngredient, DishProfile, slugify
from pantrypath_core.result import (
    DishInfo,
    IngredientResult,
    Meta,
    Product,
    RequestInfo,
    Result,
    Store,
    Summary,
)


def ingredient(name, role="core", category="dairy", share=0.8, **kw):
    return CatalogIngredient(name=name, role=role, category=category, share=share, **kw)


def dish(**overrides):
    data = dict(
        dish_id="quiche-lorraine", name="Quiche Lorraine", cuisine="european",
        ingredients=[ingredient("eggs", category="egg"), ingredient("heavy cream"),
                     ingredient("pie crust", category="bakery", share=0.0, added_by_llm=True)],
        recipe_count=40, review_count=900, source="foodcom", catalog_version="2026-10-24",
    )
    data.update(overrides)
    return DishProfile(**data)


def test_dish_profile_normalizes_names():
    d = dish(aliases=["Quiche  Lorraine", "Bacon Quiche"])
    assert d.name == "quiche lorraine"
    assert d.aliases == ["bacon quiche"]


@pytest.mark.parametrize("overrides", [
    {"dish_id": "Quiche Lorraine"},
    {"ingredients": [ingredient("eggs")]},
    {"ingredients": [ingredient("eggs"), ingredient("eggs"), ingredient("milk")]},
    {"ingredients": [ingredient(n, role="common") for n in ("a", "b", "c")]},
    {"recipe_count": -1},
])
def test_dish_profile_rejects_bad_input(overrides):
    with pytest.raises(ValidationError):
        dish(**overrides)


def test_ingredient_share_must_be_a_fraction():
    with pytest.raises(ValidationError):
        ingredient("eggs", share=1.5)


def test_catalog_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        Catalog(catalog_version="v", generated_at=datetime.now(UTC), dishes=[dish(), dish()])


def test_slugify():
    assert slugify("Mom's  Chicken Pot-Pie!") == "mom-s-chicken-pot-pie"


def make_result(products, stores=None, summary=None, expires_in=timedelta(hours=24)):
    now = datetime.now(UTC)
    ingredients = [IngredientResult(name="eggs", role="core", category="egg", products=products)]
    return Result(
        request=RequestInfo(query="quiche", zip="45202", avoid=["milk"], diet="vegetarian"),
        dish=DishInfo(name="quiche lorraine", cuisine="european", source="catalog"),
        stores=stores if stores is not None else [Store(
            store_id="01400943", chain="KROGER", name="Kroger", address="1 Main St",
            lat=39.1, lon=-84.5, distance_miles=1.2)],
        ingredients=ingredients,
        summary=summary or Summary.from_ingredients(ingredients),
        meta=Meta(generated_at=now, expires_at=now + expires_in, providers_used=["kroger"],
                  catalog_version="2026-10-24"),
    )


def product(verdict="safe", store_id="01400943"):
    return Product(store_id=store_id, barcode="0001111060903", name="Large Eggs",
                   verdict=verdict, reason="No milk", evidence_source="kroger")


def test_result_round_trip():
    result = make_result([product()])
    again = Result.model_validate_json(result.model_dump_json())
    assert again == result
    assert again.summary.with_safe_product == 1
    assert again.meta.schema_version == "1.0"


def test_summary_lists_ingredients_without_safe_products():
    result = make_result([product("unknown"), product("unsafe")])
    assert result.summary.without_safe_product == ["eggs"]


@pytest.mark.parametrize("kwargs", [
    {"stores": []},
    {"summary": Summary(ingredients_total=1, with_safe_product=0, without_safe_product=[])},
    {"expires_in": timedelta(0)},
])
def test_result_rejects_inconsistent_data(kwargs):
    with pytest.raises(ValidationError):
        make_result([product()], **kwargs)


def test_request_rejects_bad_zip_and_allergen():
    with pytest.raises(ValidationError):
        RequestInfo(query="quiche", zip="4520")
    with pytest.raises(ValidationError):
        RequestInfo(query="quiche", zip="45202", avoid=["mustard"])
