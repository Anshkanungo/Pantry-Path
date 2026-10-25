"""result.json contract v1.0: the data pipeline's answer to one request.

Every product gets a verdict and a reason; the next stage (a teammate's) shows only
safe products but can explain the rest. The file is temporary (expires_at) because
Kroger's terms forbid keeping copies of its data.

Two clarifications of the shared draft: `barcode` and `in_stock` may be null when a
store doesn't report them (e.g. a provider without barcodes, or no stock level).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from pantrypath_core.catalog import Cuisine, Role
from pantrypath_core.diet_rules import Allergen, Diet, EvidenceSource, VerdictKind
from pantrypath_core.schema import Category

RESULT_SCHEMA_VERSION = "1.0"


class RequestInfo(BaseModel):
    query: str = Field(min_length=1)
    zip: str = Field(pattern=r"^\d{5}$")
    avoid: list[Allergen] = Field(default_factory=list)
    diet: Diet = Diet.none


class DishInfo(BaseModel):
    name: str
    cuisine: Cuisine
    source: Literal["catalog", "llm"]


class Store(BaseModel):
    store_id: str
    chain: str
    name: str
    address: str
    lat: float
    lon: float
    distance_miles: float = Field(ge=0)


class Product(BaseModel):
    store_id: str
    barcode: str | None = Field(default=None, description="GTIN-13, digits only")
    name: str
    brand: str = ""
    size: str = ""
    in_stock: bool | None = None
    aisle: str | None = None
    verdict: VerdictKind
    reason: str
    evidence_source: EvidenceSource


class IngredientResult(BaseModel):
    name: str
    role: Role
    category: Category
    products: list[Product] = Field(default_factory=list)


class Summary(BaseModel):
    ingredients_total: int = Field(ge=0)
    with_safe_product: int = Field(ge=0)
    without_safe_product: list[str] = Field(default_factory=list)

    @classmethod
    def from_ingredients(cls, ingredients: list[IngredientResult]) -> "Summary":
        missing = [
            i.name for i in ingredients
            if not any(p.verdict == VerdictKind.safe for p in i.products)
        ]
        return cls(
            ingredients_total=len(ingredients),
            with_safe_product=len(ingredients) - len(missing),
            without_safe_product=missing,
        )


class Meta(BaseModel):
    generated_at: datetime
    expires_at: datetime
    providers_used: list[str] = Field(default_factory=list)
    catalog_version: str
    schema_version: Literal["1.0"] = RESULT_SCHEMA_VERSION


class Result(BaseModel):
    request: RequestInfo
    dish: DishInfo
    stores: list[Store]
    ingredients: list[IngredientResult]
    summary: Summary
    meta: Meta

    @model_validator(mode="after")
    def _consistent(self) -> "Result":
        if self.summary != Summary.from_ingredients(self.ingredients):
            raise ValueError("summary does not match the ingredients")
        store_ids = {s.store_id for s in self.stores}
        unknown = {
            p.store_id for i in self.ingredients for p in i.products if p.store_id not in store_ids
        }
        if unknown:
            raise ValueError(f"products reference stores not in the result: {sorted(unknown)}")
        if self.meta.expires_at <= self.meta.generated_at:
            raise ValueError("expires_at must be after generated_at")
        return self
