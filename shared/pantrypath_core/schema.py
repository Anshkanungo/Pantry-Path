"""Recipe and ingredient schema: the contract between the model output,
the diet rules engine and the product matcher.

The schema only describes what is in a recipe. It deliberately has no
allergen or diet fields: diet safety is decided by the rules engine from
product data, never by whoever wrote the recipe JSON.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "0.1"


class Unit(str, Enum):
    # Mass
    g = "g"
    kg = "kg"
    oz = "oz"
    lb = "lb"
    # Volume
    ml = "ml"
    l = "l"  # noqa: E741 - the unit name for litre
    tsp = "tsp"
    tbsp = "tbsp"
    cup = "cup"
    # Count-like
    piece = "piece"
    clove = "clove"
    slice = "slice"
    can = "can"
    pinch = "pinch"


class Category(str, Enum):
    """Coarse grocery category, used to narrow product searches."""

    produce = "produce"
    dairy = "dairy"
    egg = "egg"
    meat = "meat"
    seafood = "seafood"
    grain = "grain"
    bakery = "bakery"
    legume = "legume"
    nut_seed = "nut_seed"
    oil_fat = "oil_fat"
    spice_herb = "spice_herb"
    condiment = "condiment"
    sweetener = "sweetener"
    baking = "baking"
    beverage = "beverage"
    other = "other"


class Ingredient(BaseModel):
    raw: str = Field(description='Original line, e.g. "1 cup shredded Gruyere cheese"')
    name: str = Field(description='Canonical searchable name, e.g. "gruyere cheese"')
    quantity: float | None = Field(default=None, gt=0, description="None for 'to taste'")
    unit: Unit | None = Field(
        default=None, description="None when the item is counted bare, e.g. '3 eggs' -> piece"
    )
    form: str | None = Field(default=None, description='Preparation, e.g. "shredded", "diced"')
    category: Category
    optional: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        v = " ".join(v.lower().split())
        if not v:
            raise ValueError("name must not be empty")
        return v

    @model_validator(mode="after")
    def unit_needs_quantity(self) -> "Ingredient":
        if self.unit is not None and self.quantity is None:
            raise ValueError(f"'{self.name}': unit given without quantity")
        return self


class Recipe(BaseModel):
    schema_version: Literal["0.1"] = SCHEMA_VERSION
    dish: str
    servings: int = Field(gt=0)
    ingredients: list[Ingredient] = Field(min_length=1)
    source: Literal["golden_manual", "llm", "recipenlg", "foodcom"] = Field(
        description="Where this record came from, so we can trace and filter it later"
    )

    @model_validator(mode="after")
    def no_duplicate_ingredients(self) -> "Recipe":
        names = [i.name for i in self.ingredients]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate ingredients: {sorted(dupes)}")
        return self
