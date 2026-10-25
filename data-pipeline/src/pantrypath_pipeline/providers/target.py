"""Target provider via RedCircle API. EXPERIMENTAL, fallback only.

Used only when Kroger has no store in range, or no in-stock product for an
ingredient. RedCircle bills 1 credit per call (the free plan has 100 in total) and
has no store locator and no allergen fields, so this provider is deliberately frugal:

- the nearest Target comes from one search + one product lookup (`location_info`);
- per ingredient: one search, plus product details (barcode, ingredients, aisle,
  stock) for only the first `details_per_term` results; the rest stay "unknown";
- a credit budget per provider instance (= per request) stops further calls.

Verified on 2026-10-24 with two live calls: search results carry tcin/title/brand
only; product details carry a 12-digit UPC, the ingredient text, aisle and
`location_info` (no coordinates, so a store's position is its ZIP's centre).
RedCircle's storage terms are unverified, so nothing is written to disk.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from pantrypath_core.barcodes import to_gtin13
from pantrypath_core.diet_rules import ProductEvidence
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

SIZE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:fl\.? oz|oz|lbs?|ct|count|gal|qt|pt|kg|g|ml|l)\b", re.IGNORECASE
)


class CreditBudgetError(ProviderError):
    """This request already spent its RedCircle credit budget."""


def size_from_title(title: str) -> str:
    """'Heavy Whipping Cream - 32 fl oz - Good & Gather' -> '32 fl oz'."""
    match = SIZE_PATTERN.search(title)
    return match.group(0) if match else ""


class TargetProvider(StoreProvider):
    """Target stores through RedCircle: the nearest store to the ZIP only."""

    name = "target"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://api.redcircleapi.com/request",
        session: Any = None,
        timeout: float = 60,
        attempts: int = 2,
        backoff_s: float = 0.5,
        details_per_term: int = 1,
        max_credits: int = 30,
        probe_term: str = "milk",
        cache_ttl_s: float = 900,
        zip_locator: ZipLocator | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        if not api_key:
            raise ProviderError(
                "RedCircle key missing: set REDCIRCLE_API_KEY in .env to use the Target fallback"
            )
        self._api_key = api_key
        self.base_url = base_url
        self.session = session or requests.Session()
        self.timeout, self.attempts, self.backoff_s = timeout, attempts, backoff_s
        self.details_per_term = details_per_term
        self.max_credits = max_credits
        self.probe_term = probe_term
        self.zip_locator = zip_locator or (lambda z: zip_centroid(z, self.session))
        self._cache = TTLCache(cache_ttl_s, clock)
        self._store_zip: dict[str, str] = {}  # store_id -> the customer ZIP it was found for
        self._credits_used = 0
        self._budget_lock = threading.Lock()
        self._budget_warned = False

    @classmethod
    def from_config(cls, cfg: Config) -> "TargetProvider":
        request, settings = cfg["request"], cfg["providers"]["target"]
        return cls(
            secret("REDCIRCLE_API_KEY"),
            base_url=settings.get("base_url", "https://api.redcircleapi.com/request"),
            timeout=max(60, request.get("http_timeout_s", 15)),  # RedCircle scrapes live
            attempts=min(2, request.get("http_attempts", 3)),  # every retry costs a credit
            details_per_term=settings.get("details_per_term", 1),
            max_credits=settings.get("max_credits_per_request", 30),
            probe_term=settings.get("store_probe_term", "milk"),
            cache_ttl_s=request.get("cache_ttl_s", 900),
        )

    @property
    def credits_used(self) -> int:
        return self._credits_used

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("type", "?")
        with self._budget_lock:
            if self._credits_used >= self.max_credits:
                raise CreditBudgetError(
                    f"RedCircle budget of {self.max_credits} credits for this request is spent"
                )
            self._credits_used += 1
        response = http_call(
            self.session, "GET", self.base_url, what=f"RedCircle {kind}",
            attempts=self.attempts, timeout=self.timeout, backoff_s=self.backoff_s,
            params={"api_key": self._api_key, **params},
        )
        self._count_call()
        if response.status_code != 200:
            raise ProviderError(f"RedCircle {kind} failed: HTTP {response.status_code}")
        body = response.json()
        info = body.get("request_info") or {}
        if info.get("success") is False:
            reason = info.get("message", "no reason given")
            raise ProviderError(f"RedCircle {kind} failed: {reason}")
        log.debug("RedCircle %s: %s credits left", kind, info.get("credits_remaining"))
        return body

    def _warn_budget(self, exc: CreditBudgetError) -> None:
        if not self._budget_warned:
            log.warning("%s; remaining Target searches are skipped", exc)
            self._budget_warned = True

    def find_stores(self, zip_code: str, radius_miles: float, limit: int) -> list[Store]:
        key = ("stores", zip_code, radius_miles)
        if (cached := self._cache.get(key)) is not None:
            return cached[:limit]
        try:
            search = self._request({
                "type": "search", "search_term": self.probe_term,
                "customer_zipcode": zip_code, "delivery_type": "buy_at_store",
            })
            results = search.get("search_results") or []
            if not results:
                return []
            detail = self._request({
                "type": "product", "tcin": results[0]["product"]["tcin"],
                "customer_zipcode": zip_code,
            })
        except CreditBudgetError as exc:
            self._warn_budget(exc)
            return []
        location = detail.get("location_info")
        if not location or not location.get("store_id"):
            log.info("Target: no store with in-store shopping near %s", zip_code)
            return []
        store = self._to_store(location, zip_code)
        stores = [store] if store.distance_miles <= radius_miles else []
        if stores:
            self._store_zip[store.store_id] = zip_code
        log.info("Target: %d store(s) within %s miles of %s", len(stores), radius_miles, zip_code)
        self._cache.set(key, stores)
        return stores[:limit]

    def _to_store(self, location: dict[str, Any], customer_zip: str) -> Store:
        store_zip = (location.get("zipcode") or customer_zip)[:5]
        lat, lon = self.zip_locator(store_zip)  # RedCircle gives no coordinates
        origin = self.zip_locator(customer_zip)
        state_zip = " ".join(p for p in (location.get("state"), location.get("zipcode")) if p)
        parts = (location.get("address"), location.get("city"), state_zip)
        address = ", ".join(p for p in parts if p)
        return Store(
            store_id=f"target-{location['store_id']}",
            chain="TARGET",
            name=f"Target {location.get('store_name') or ''}".strip(),
            address=address,
            lat=lat,
            lon=lon,
            distance_miles=round(haversine_miles(origin[0], origin[1], lat, lon), 2),
        )

    def search_products(self, term: str, store: Store, limit: int) -> list[ProviderProduct]:
        zip_code = self._store_zip.get(store.store_id)
        if zip_code is None:
            raise ProviderError(f"Target store {store.store_id} was not found by find_stores")
        key = ("products", term.lower(), store.store_id, limit)
        if (cached := self._cache.get(key)) is not None:
            return cached
        try:
            body = self._request({
                "type": "search", "search_term": term,
                "customer_zipcode": zip_code, "delivery_type": "buy_at_store",
            })
        except CreditBudgetError as exc:
            self._warn_budget(exc)
            return []
        products = []
        for position, result in enumerate((body.get("search_results") or [])[:limit]):
            summary = result.get("product") or {}
            detail: dict[str, Any] = {}
            if position < self.details_per_term and summary.get("tcin"):
                try:
                    detail = self._request({
                        "type": "product", "tcin": summary["tcin"], "customer_zipcode": zip_code,
                    }).get("product") or {}
                except CreditBudgetError as exc:
                    self._warn_budget(exc)
            products.append(self._to_product(summary, detail, store.store_id))
        self._cache.set(key, products)
        return products

    def _to_product(
        self, summary: dict[str, Any], detail: dict[str, Any], store_id: str
    ) -> ProviderProduct:
        title = (detail.get("title") or summary.get("title") or "").strip()
        brand = (detail.get("brand") or summary.get("brand") or "").strip()
        if not detail:  # search results alone have no barcode, ingredients or stock
            return ProviderProduct(
                provider=self.name, store_id=store_id, barcode=None, name=title, brand=brand,
                size=size_from_title(title),
            )
        offer = detail.get("buybox_winner") or {}
        pickup = (offer.get("fulfillment") or {}).get("pickup_info") or {}
        if pickup.get("stock_level") is not None:
            in_stock = float(pickup["stock_level"]) > 0
        else:
            in_stock = (offer.get("availability") or {}).get("in_stock")
        categories = [c.strip() for c in (detail.get("categories_flat") or "").split(">")]
        return ProviderProduct(
            provider=self.name,
            store_id=store_id,
            barcode=to_gtin13(detail.get("upc")),
            name=title,
            brand=brand,
            size=size_from_title(title),
            in_stock=in_stock,
            aisle=detail.get("aisle") or None,
            evidence=ProductEvidence(
                source="target", name=title, brand=brand,
                categories=[c for c in categories if c],
                ingredients_text=(detail.get("ingredients") or "").strip(),
            ),
        )
