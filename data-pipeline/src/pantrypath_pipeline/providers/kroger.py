"""Kroger provider: nearby stores and in-store products from Kroger's public API.

Kroger's terms forbid building databases or keeping copies of their data beyond the
cache header, and they send none: responses stay in memory for one process only.

Allergen evidence comes from Kroger's own fields: the `allergens` list ("Contains",
"May contain", "Free from", ...), else the `allergensDescription` sentence, plus the
ingredient statement in `nutritionInformation`. Kroger names allergens in EU style
("Milk and its derivates", "Cereals and Their Derivatives"); they are mapped to
pantrypath_core's Allergen values, written as words ("tree nuts") for the diet rules.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from pantrypath_core.barcodes import kroger_upc_to_gtin13
from pantrypath_core.diet_rules import Allergen, ProductEvidence
from pantrypath_core.result import Store
from pantrypath_pipeline.config import Config, secret
from pantrypath_pipeline.errors import ProviderError
from pantrypath_pipeline.providers.base import (
    ProviderProduct,
    StoreProvider,
    TTLCache,
    ZipLocator,
    haversine_miles,
    http_call,
    zip_centroid,
)

log = logging.getLogger(__name__)

# Kroger allergen name keyword -> Allergen; first match wins, so specific words go first.
# Not mapped on purpose: oats (not gluten by itself; the ingredient text still shows
# them), coconut (no longer a tree nut for FDA labeling), lupine, celery, sulfites.
ALLERGEN_KEYWORDS: tuple[tuple[str, Allergen | None], ...] = (
    ("does not contain", None),
    ("buckwheat", None),  # gluten-free despite the name
    ("coconut", None),
    ("peanut", Allergen.peanuts),
    ("tree nut", Allergen.tree_nuts),
    ("almond", Allergen.tree_nuts),
    ("brazil nut", Allergen.tree_nuts),
    ("cashew", Allergen.tree_nuts),
    ("chestnut", Allergen.tree_nuts),
    ("hazelnut", Allergen.tree_nuts),
    ("hickory", Allergen.tree_nuts),
    ("macadamia", Allergen.tree_nuts),
    ("pecan", Allergen.tree_nuts),
    ("pistachio", Allergen.tree_nuts),
    ("walnut", Allergen.tree_nuts),
    ("shellfish", Allergen.shellfish),
    ("crustacean", Allergen.shellfish),
    ("shrimp", Allergen.shellfish),
    ("crab", Allergen.shellfish),
    ("lobster", Allergen.shellfish),
    ("mollus", Allergen.shellfish),  # not a US major allergen; shellfish-allergic people avoid it
    ("fish", Allergen.fish),
    ("tuna", Allergen.fish),
    ("salmon", Allergen.fish),
    ("anchov", Allergen.fish),
    ("milk", Allergen.milk),
    ("lactose", Allergen.milk),
    ("egg", Allergen.egg),
    ("soy", Allergen.soy),
    ("sesame", Allergen.sesame),
    ("wheat", Allergen.wheat),
    ("gluten", Allergen.gluten),
    ("cereal", Allergen.gluten),  # Kroger's "Cereals and Their Derivatives" = cereals with gluten
    ("barley", Allergen.gluten),
    ("rye", Allergen.gluten),
    ("spelt", Allergen.gluten),
)
CONTAINS_LEVELS = {"contains", "derived from"}
MAY_CONTAIN_LEVELS = {"may contain"}
STOCK_LEVELS = {"HIGH": True, "LOW": True, "TEMPORARILY_OUT_OF_STOCK": False}
TOKEN_MARGIN_S = 60  # renew the token a minute before Kroger says it expires


def allergen_from_name(name: str) -> Allergen | None:
    """'Almond And Almond Products' -> Allergen.tree_nuts; unknown names -> None."""
    text = " ".join(name.lower().split())
    for keyword, allergen in ALLERGEN_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}", text):
            return allergen
    return None


def allergen_word(allergen: Allergen) -> str:
    return allergen.value.replace("_", " ")


def parse_allergens_description(text: str) -> tuple[set[Allergen], set[Allergen]]:
    """'Contains Wheat ...,Milk ... . May contain Peanuts ...' -> (contains, may_contain)."""
    contains: set[Allergen] = set()
    may_contain: set[Allergen] = set()
    for sentence in re.split(r"\.\s*", text):
        lowered = sentence.strip().lower()
        if lowered.startswith("may contain"):
            target, rest = may_contain, sentence.strip()[len("may contain"):]
        elif lowered.startswith("contains"):
            target, rest = contains, sentence.strip()[len("contains"):]
        else:
            continue
        for part in rest.strip(" :").split(","):
            if (allergen := allergen_from_name(part)) is not None:
                target.add(allergen)
    return contains, may_contain


def kroger_evidence(raw: dict[str, Any]) -> ProductEvidence:
    """Kroger's own allergen and ingredient data for one product."""
    contains: set[Allergen] = set()
    may_contain: set[Allergen] = set()
    for entry in raw.get("allergens") or []:
        allergen = allergen_from_name(entry.get("name") or "")
        level = (entry.get("levelOfContainmentName") or "").strip().lower()
        if allergen is None:
            continue
        if level in CONTAINS_LEVELS:
            contains.add(allergen)
        elif level in MAY_CONTAIN_LEVELS:
            may_contain.add(allergen)
    if not contains and not may_contain and raw.get("allergensDescription"):
        contains, may_contain = parse_allergens_description(raw["allergensDescription"])
    statement = next(
        (n["ingredientStatement"] for n in raw.get("nutritionInformation") or []
         if n and n.get("ingredientStatement")),
        "",
    )
    return ProductEvidence(
        source="kroger",
        name=(raw.get("description") or "").strip(),
        brand=(raw.get("brand") or "").strip(),
        categories=[c for c in raw.get("categories") or [] if c],
        ingredients_text=statement.strip(),
        allergens=sorted(allergen_word(a) for a in contains),
        traces=sorted(allergen_word(a) for a in may_contain),
    )


def aisle_text(locations: list[dict[str, Any]] | None) -> str | None:
    """[{'description': 'DAIRY', 'number': '100'}] -> 'Dairy, aisle 100'."""
    for loc in locations or []:
        description = (loc.get("description") or "").strip().title()
        number = (loc.get("number") or "").strip()
        if description and number:
            return f"{description}, aisle {number}"
        if description or number:
            return description or f"aisle {number}"
    return None


class KrogerProvider(StoreProvider):
    """Kroger family stores (Kroger, Ralphs, Harris Teeter, Fred Meyer, ...)."""

    name = "kroger"

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        *,
        base_url: str = "https://api.kroger.com/v1",
        scope: str = "product.compact",
        session: Any = None,
        timeout: float = 15,
        attempts: int = 3,
        backoff_s: float = 0.5,
        cache_ttl_s: float = 900,
        locations_fetch: int = 50,
        zip_locator: ZipLocator | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        if not client_id or not client_secret:
            raise ProviderError(
                "Kroger credentials missing: set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET "
                "in .env (free keys at developer.kroger.com)"
            )
        self._client_id, self._client_secret = client_id, client_secret
        self.base_url = base_url.rstrip("/")
        self.scope = scope
        self.session = session or requests.Session()
        self.timeout, self.attempts, self.backoff_s = timeout, attempts, backoff_s
        self.locations_fetch = locations_fetch
        self.zip_locator = zip_locator or (lambda z: zip_centroid(z, self.session))
        self._clock = clock
        self._cache = TTLCache(cache_ttl_s, clock)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> "KrogerProvider":
        request, settings = cfg["request"], cfg["providers"]["kroger"]
        return cls(
            secret("KROGER_CLIENT_ID"),
            secret("KROGER_CLIENT_SECRET"),
            base_url=settings.get("base_url", "https://api.kroger.com/v1"),
            scope=settings.get("scope", "product.compact"),
            timeout=request.get("http_timeout_s", 15),
            attempts=request.get("http_attempts", 3),
            cache_ttl_s=request.get("cache_ttl_s", 900),
            locations_fetch=settings.get("locations_fetch", 50),
        )

    def _access_token(self) -> str:
        """OAuth2 client-credentials token, kept in memory until shortly before it expires."""
        with self._token_lock:
            if self._token and self._clock() < self._token_expires - TOKEN_MARGIN_S:
                return self._token
            response = http_call(
                self.session, "POST", f"{self.base_url}/connect/oauth2/token",
                what="Kroger token request", attempts=self.attempts, timeout=self.timeout,
                backoff_s=self.backoff_s, auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials", "scope": self.scope},
            )
            self._count_call()
            if response.status_code != 200:
                raise ProviderError(
                    f"Kroger token request failed: HTTP {response.status_code}; check "
                    "KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in .env"
                )
            body = response.json()
            self._token = body["access_token"]
            self._token_expires = self._clock() + float(body.get("expires_in", 1800))
            return self._token

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            response = http_call(
                self.session, "GET", f"{self.base_url}{path}", what=f"Kroger {path}",
                attempts=self.attempts, timeout=self.timeout, backoff_s=self.backoff_s,
                params=params,
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "Accept": "application/json"},
            )
            self._count_call()
            if response.status_code == 401 and attempt == 1:
                with self._token_lock:  # token revoked or expired early: get a new one, once
                    self._token = None
                continue
            if response.status_code != 200:
                raise ProviderError(
                    f"Kroger {path} failed: HTTP {response.status_code} {response.text[:200]}"
                )
            return response.json()
        raise ProviderError(f"Kroger {path} kept answering 401 with a fresh token; check the keys")

    def find_stores(self, zip_code: str, radius_miles: float, limit: int) -> list[Store]:
        key = ("stores", zip_code, radius_miles, limit)
        if (cached := self._cache.get(key)) is not None:
            return cached
        body = self._get("/locations", {
            "filter.zipCode.near": zip_code,
            "filter.radiusInMiles": max(1, min(100, round(radius_miles))),
            "filter.limit": self.locations_fetch,
        })
        origin = self.zip_locator(zip_code)
        stores = []
        for location in body.get("data") or []:
            # Warehouses and "forecast sheds" are listed as locations too; real stores
            # have departments (deli, pharmacy, pickup, ...)
            if not location.get("departments"):
                continue
            store = self._to_store(location, origin)
            if store is not None and store.distance_miles <= radius_miles:
                stores.append(store)
        stores.sort(key=lambda s: s.distance_miles)
        stores = stores[:limit]
        log.info("Kroger: %d store(s) within %s miles of %s", len(stores), radius_miles, zip_code)
        self._cache.set(key, stores)
        return stores

    @staticmethod
    def _to_store(location: dict[str, Any], origin: tuple[float, float]) -> Store | None:
        geo = location.get("geolocation") or {}
        if geo.get("latitude") is None or geo.get("longitude") is None:
            log.debug("Kroger location %s has no coordinates; skipped", location.get("locationId"))
            return None
        lat, lon = float(geo["latitude"]), float(geo["longitude"])
        address = location.get("address") or {}
        street = ", ".join(p for p in (address.get("addressLine1"), address.get("city")) if p)
        state_zip = " ".join(p for p in (address.get("state"), address.get("zipCode")) if p)
        return Store(
            store_id=location["locationId"],
            chain=location.get("chain") or "KROGER",
            name=location.get("name") or "Kroger",
            address=", ".join(p for p in (street, state_zip) if p),
            lat=lat,
            lon=lon,
            distance_miles=round(haversine_miles(origin[0], origin[1], lat, lon), 2),
        )

    def search_products(self, term: str, store: Store, limit: int) -> list[ProviderProduct]:
        key = ("products", term.lower(), store.store_id, limit)
        if (cached := self._cache.get(key)) is not None:
            return cached
        body = self._get("/products", {
            "filter.term": term, "filter.locationId": store.store_id, "filter.limit": limit,
        })
        products = [self._to_product(raw, store.store_id) for raw in body.get("data") or []]
        self._cache.set(key, products)
        return products[:limit]

    def _to_product(self, raw: dict[str, Any], store_id: str) -> ProviderProduct:
        item = (raw.get("items") or [{}])[0] or {}
        stock_level = (item.get("inventory") or {}).get("stockLevel")
        return ProviderProduct(
            provider=self.name,
            store_id=store_id,
            barcode=kroger_upc_to_gtin13(raw.get("upc")),
            name=(raw.get("description") or "").strip(),
            brand=(raw.get("brand") or "").strip(),
            size=(item.get("size") or "").strip(),
            in_stock=STOCK_LEVELS.get(stock_level),
            aisle=aisle_text(raw.get("aisleLocations")),
            evidence=kroger_evidence(raw),
        )
