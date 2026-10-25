"""Store providers: the common interface for Kroger, Target and any later chain.

A provider finds stores near a ZIP and searches one store's products. Every provider
returns the same ProviderProduct shape (GTIN-13 barcode, stock, aisle, and the
store's own allergen/ingredient data as ProductEvidence), so the request flow
doesn't care which chain the data came from.

Adding a chain = one StoreProvider subclass + one `providers.<name>` entry (with its
`class`) in config/pipeline.yaml, then its name in `request.providers`.

Nothing a provider returns is written to disk: Kroger's terms forbid keeping copies
of their data. Caching is in memory only (TTLCache) and ends with the process.
"""

import importlib
import logging
import math
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pantrypath_core.diet_rules import ProductEvidence
from pantrypath_core.result import Store
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import ProviderError

log = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8
ZIPPOPOTAM_URL = "https://api.zippopotam.us/us/{zip}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

ZipLocator = Callable[[str], tuple[float, float]]


@dataclass(frozen=True)
class ProviderProduct:
    """One product at one store, in the same shape for every chain."""

    provider: str
    store_id: str
    barcode: str | None  # GTIN-13, or None when the store gives no usable barcode
    name: str
    brand: str = ""
    size: str = ""
    in_stock: bool | None = None  # None: the store didn't say
    aisle: str | None = None
    evidence: ProductEvidence | None = None  # the store's own allergen/ingredient data


class StoreProvider(ABC):
    """A grocery chain's API: stores near a ZIP, then products at one store."""

    name: str = ""

    def __init__(self) -> None:
        self._calls = 0
        self._calls_lock = threading.Lock()

    @property
    def calls(self) -> int:
        """HTTP calls made so far (including auth), for the request summary."""
        return self._calls

    def _count_call(self) -> None:
        with self._calls_lock:
            self._calls += 1

    @classmethod
    @abstractmethod
    def from_config(cls, cfg: Config) -> "StoreProvider":
        """Build the provider from pipeline.yaml and .env; ProviderError if a key is missing."""

    @abstractmethod
    def find_stores(self, zip_code: str, radius_miles: float, limit: int) -> list[Store]:
        """Stores within radius_miles of the ZIP, nearest first, at most `limit`."""

    @abstractmethod
    def search_products(self, term: str, store: Store, limit: int) -> list[ProviderProduct]:
        """Products matching `term` at one store, most relevant first, at most `limit`."""


def make_providers(cfg: Config, names: list[str] | None = None) -> list[StoreProvider]:
    """Providers in request.providers order. One without its keys is skipped with a warning."""
    providers: list[StoreProvider] = []
    problems: list[str] = []
    for name in names or cfg["request"]["providers"]:
        settings = cfg["providers"].get(name) or {}
        if "class" not in settings:
            raise ProviderError(
                f"provider {name!r} has no class: add providers.{name}.class to "
                "config/pipeline.yaml"
            )
        module_name, class_name = settings["class"].split(":")
        provider_class = getattr(importlib.import_module(module_name), class_name)
        try:
            providers.append(provider_class.from_config(cfg))
        except ProviderError as exc:
            problems.append(str(exc))
            log.warning("Store provider %s is disabled: %s", name, exc)
    if not providers:
        raise ProviderError("no store provider could be set up: " + "; ".join(problems))
    return providers


class TTLCache:
    """A small thread-safe in-memory cache whose entries expire after ttl_s seconds."""

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_s
        self._clock = clock
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires_at, value = hit
            if self._clock() >= expires_at:
                del self._data[key]
                return None
            return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl_s <= 0:
            return
        with self._lock:
            self._data[key] = (self._clock() + self.ttl_s, value)


class TransientHTTPError(Exception):
    """A failure worth retrying: HTTP 429, 5xx, a timeout or a dropped connection."""


def http_call(
    session: Any,
    method: str,
    url: str,
    *,
    what: str,
    attempts: int = 3,
    timeout: float = 15,
    backoff_s: float = 0.5,
    **kwargs: Any,
) -> requests.Response:
    """Send one HTTP request, retrying transient failures with exponential backoff.

    Returns the response for any status except 429/5xx; the caller checks the status.
    Error messages name `what` and the exception type only: request URLs can carry
    API keys (RedCircle takes its key as a query parameter) and must never be logged.
    """

    @retry(
        retry=retry_if_exception_type(TransientHTTPError),
        stop=stop_after_attempt(max(1, attempts)),
        wait=wait_exponential(multiplier=backoff_s, max=8),
        reraise=True,
    )
    def once() -> requests.Response:
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientHTTPError(type(exc).__name__) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientHTTPError(f"HTTP {response.status_code}")
        return response

    try:
        return once()
    except TransientHTTPError as exc:
        raise ProviderError(
            f"{what} failed after {attempts} tries ({exc}); the service may be down "
            "or rate-limiting us"
        ) from None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


_zip_cache: dict[str, tuple[float, float]] = {}  # ZIP centres are public data, safe to keep
_zip_lock = threading.Lock()


def zip_centroid(zip_code: str, session: Any = None, timeout: float = 10) -> tuple[float, float]:
    """(lat, lon) of a US ZIP's centre: zippopotam.us, else OpenStreetMap Nominatim.

    Both are free and need no key. Raises ProviderError when neither knows the ZIP.
    """
    with _zip_lock:
        if zip_code in _zip_cache:
            return _zip_cache[zip_code]
    session = session or requests.Session()
    problems = []
    try:
        response = session.request("GET", ZIPPOPOTAM_URL.format(zip=zip_code), timeout=timeout)
        if response.status_code == 200:
            place = response.json()["places"][0]
            point = (float(place["latitude"]), float(place["longitude"]))
            return _remember_zip(zip_code, point)
        problems.append(f"zippopotam.us HTTP {response.status_code}")
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        problems.append(f"zippopotam.us {type(exc).__name__}")
    try:
        user_agent = (
            os.environ.get("APP_USER_AGENT") or os.environ.get("OFF_USER_AGENT")
            or "PantryPath/0.1 (data pipeline)"
        )
        response = session.request(
            "GET", NOMINATIM_URL, timeout=timeout, headers={"User-Agent": user_agent},
            params={"postalcode": zip_code, "country": "us", "format": "json", "limit": 1},
        )
        if response.status_code != 200:
            problems.append(f"Nominatim HTTP {response.status_code}")
        elif hits := response.json():
            return _remember_zip(zip_code, (float(hits[0]["lat"]), float(hits[0]["lon"])))
        else:
            problems.append("Nominatim: no match")
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        problems.append(f"Nominatim {type(exc).__name__}")
    raise ProviderError(
        f"could not locate ZIP {zip_code} ({'; '.join(problems)}); check the ZIP and the network"
    )


def _remember_zip(zip_code: str, point: tuple[float, float]) -> tuple[float, float]:
    with _zip_lock:
        _zip_cache[zip_code] = point
    return point
