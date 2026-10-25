"""Dish catalog models: what the batch pipeline builds and the request flow reads.

A dish profile lists ingredients only. Quantities are left out on purpose:
Food.com quantities have no units, and amounts are the final-answer LLM's job.
"""

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from pantrypath_core.schema import Category

CATALOG_SCHEMA_VERSION = "1.0"


class Cuisine(str, Enum):
    american = "american"
    european = "european"
    latin_american = "latin_american"
    east_se_asian = "east_se_asian"
    south_asian = "south_asian"
    middle_eastern_african = "middle_eastern_african"


class Role(str, Enum):
    """core: the dish isn't itself without it. common: in many versions, but not all."""

    core = "core"
    common = "common"


def normalize_name(value: str) -> str:
    """Lowercase and collapse whitespace; reject empty names."""
    value = " ".join(value.lower().split())
    if not value:
        raise ValueError("name must not be empty")
    return value


def slugify(name: str) -> str:
    """'Chicken Pot Pie' -> 'chicken-pot-pie' (used as dish_id)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class CatalogIngredient(BaseModel):
    name: str = Field(description='Canonical searchable name, e.g. "pie crust"')
    role: Role
    category: Category
    share: float = Field(
        ge=0, le=1,
        description="Fraction of the dish's source recipes that list it (0 when added by the LLM)",
    )
    added_by_llm: bool = False

    @field_validator("name")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_name(v)


class DishProfile(BaseModel):
    dish_id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str
    aliases: list[str] = Field(default_factory=list)
    cuisine: Cuisine
    ingredients: list[CatalogIngredient] = Field(min_length=3)
    recipe_count: int = Field(ge=0, description="Food.com recipes grouped into this dish")
    review_count: int = Field(ge=0, description="Food.com reviews across those recipes")
    source: Literal["foodcom", "llm"]
    catalog_version: str

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        return normalize_name(v)

    @field_validator("aliases")
    @classmethod
    def _normalize_aliases(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for alias in v:
            alias = normalize_name(alias)
            if alias not in out:
                out.append(alias)
        return out

    @model_validator(mode="after")
    def _check_ingredients(self) -> "DishProfile":
        names = [i.name for i in self.ingredients]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"{self.dish_id}: duplicate ingredients {dupes}")
        if not any(i.role == Role.core for i in self.ingredients):
            raise ValueError(f"{self.dish_id}: needs at least one core ingredient")
        self.aliases = [a for a in self.aliases if a != self.name]
        return self


class Catalog(BaseModel):
    schema_version: str = CATALOG_SCHEMA_VERSION
    catalog_version: str
    generated_at: datetime
    dishes: list[DishProfile]

    @model_validator(mode="after")
    def _unique_ids(self) -> "Catalog":
        ids = [d.dish_id for d in self.dishes]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate dish_id values: {dupes}")
        return self
