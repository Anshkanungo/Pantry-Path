"""Store providers: Kroger and Target (RedCircle) against synthetic API responses.

No test touches the network. The fake sessions answer from hand-written fixtures
(tests/fixtures/req_*.json); real Kroger responses are never saved (Kroger's terms).
"""

import json
import os

import pytest
import requests

from pantrypath_core.diet_rules import Allergen, Diet, VerdictKind, assess
from pantrypath_core.result import Store
from pantrypath_pipeline.errors import ProviderError
from pantrypath_pipeline.providers import base
from pantrypath_pipeline.providers.base import (
    TTLCache,
    haversine_miles,
    http_call,
    make_providers,
    zip_centroid,
)
from pantrypath_pipeline.providers.kroger import (
    KrogerProvider,
    aisle_text,
    allergen_from_name,
    kroger_evidence,
    parse_allergens_description,
)
from pantrypath_pipeline.providers.target import TargetProvider, size_from_title

ORIGIN = (39.10, -84.51)  # the fake ZIP's centre


def load(fixtures_dir, name):
    return json.loads((fixtures_dir / name).read_text())


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeKrogerSession:
    """Answers like the Kroger API from the fixtures and records every call.

    `script` forces outcomes for the next non-token calls: an HTTP status, or "drop"
    for a dropped connection.
    """

    def __init__(self, fixtures_dir, script=()):
        self.locations = load(fixtures_dir, "req_kroger_locations.json")
        self.products = load(fixtures_dir, "req_kroger_products.json")
        self.script = list(script)
        self.calls = []
        self.tokens = 0

    def request(self, method, url, params=None, timeout=None, **kwargs):
        path = url.split("/v1", 1)[-1]
        self.calls.append((method, path, dict(params or {})))
        if path == "/connect/oauth2/token":
            self.tokens += 1
            return FakeResponse(200, {"access_token": f"token-{self.tokens}",
                                      "expires_in": 1800, "token_type": "bearer"})
        if self.script:
            outcome = self.script.pop(0)
            if outcome == "drop":
                raise requests.ConnectionError("connection dropped")
            if outcome != 200:
                return FakeResponse(outcome, {"errors": {"reason": "scripted"}})
        if path == "/locations":
            return FakeResponse(200, self.locations)
        if path == "/products":
            data = self.products.get(params["filter.term"], [])
            return FakeResponse(200, {"data": data[: params["filter.limit"]]})
        return FakeResponse(404)

    def count(self, path):
        return sum(1 for _, p, _ in self.calls if p == path)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def kroger(session, **kw):
    return KrogerProvider("id", "secret", session=session, backoff_s=0,
                          zip_locator=lambda z: ORIGIN, **kw)


STORE = Store(store_id="70100001", chain="KROGER", name="Kroger - Testville Main",
              address="1 Example St, Testville, OH 45202", lat=39.11, lon=-84.51,
              distance_miles=0.69)


# --- Kroger: stores ------------------------------------------------------------------------

def test_find_stores_keeps_real_stores_in_range_nearest_first(fixtures_dir):
    session = FakeKrogerSession(fixtures_dir)
    stores = kroger(session).find_stores("45202", 10, 3)
    # warehouse (no departments), the far store and the one without coordinates are gone
    assert [s.store_id for s in stores] == ["70100001", "70100003"]
    first = stores[0]
    assert first.chain == "KROGER"
    assert first.address == "1 Example St, Testville, OH 45202"
    assert first.distance_miles == pytest.approx(0.69, abs=0.01)
    assert stores[1].distance_miles < 10
    params = session.calls[-1][2]
    assert params["filter.zipCode.near"] == "45202" and params["filter.radiusInMiles"] == 10


def test_find_stores_respects_limit(fixtures_dir):
    assert len(kroger(FakeKrogerSession(fixtures_dir)).find_stores("45202", 10, 1)) == 1


# --- Kroger: products ----------------------------------------------------------------------

def test_search_products_maps_kroger_fields(fixtures_dir):
    products = kroger(FakeKrogerSession(fixtures_dir)).search_products("heavy cream", STORE, 5)
    cream, store_brand = products
    assert cream.provider == "kroger" and cream.store_id == "70100001"
    assert cream.barcode == "0012345000027"  # Kroger's 13 digits + computed check digit
    assert cream.name == "TestDairy Heavy Whipping Cream"
    assert (cream.brand, cream.size, cream.aisle) == ("TestDairy", "1 pt", "Dairy, aisle 100")
    assert cream.in_stock is True  # LOW still means on the shelf
    assert store_brand.in_stock is False  # TEMPORARILY_OUT_OF_STOCK
    assert store_brand.aisle is None


def test_unknown_stock_level_stays_unknown(fixtures_dir):
    [crust] = kroger(FakeKrogerSession(fixtures_dir)).search_products("pie crust", STORE, 5)
    assert crust.in_stock is None


def test_kroger_evidence_separates_contains_may_contain_and_free_from(fixtures_dir):
    raw = load(fixtures_dir, "req_kroger_products.json")["pie crust"][0]
    evidence = kroger_evidence(raw)
    assert evidence.source == "kroger"
    assert evidence.allergens == ["gluten", "wheat"]  # barley -> gluten; "Free from" dropped
    assert evidence.traces == ["soy"]
    assert evidence.ingredients_text == "ENRICHED WHEAT FLOUR, PALM OIL, SALT."
    assert evidence.name == "TestBakery Refrigerated Pie Crusts"
    assert evidence.categories == ["Baking Goods"]


def test_kroger_evidence_falls_back_to_the_description_sentence(fixtures_dir):
    raw = load(fixtures_dir, "req_kroger_products.json")["heavy cream"][1]
    evidence = kroger_evidence(raw)
    assert evidence.allergens == ["milk"]
    assert evidence.traces == ["tree nuts"]
    assert evidence.ingredients_text == ""  # nutritionInformation is null


def test_kroger_evidence_feeds_the_diet_rules(fixtures_dir):
    products = load(fixtures_dir, "req_kroger_products.json")
    cream = kroger_evidence(products["heavy cream"][0])
    eggs = kroger_evidence(products["eggs"][0])
    assert assess(cream, [Allergen.milk], Diet.none).verdict == VerdictKind.unsafe
    assert assess(eggs, [Allergen.milk], Diet.vegetarian).verdict == VerdictKind.safe
    assert assess(eggs, [], Diet.vegan).verdict == VerdictKind.unsafe


@pytest.mark.parametrize("name, expected", [
    ("Milk and its derivates", Allergen.milk),
    ("Eggs and their derivates", Allergen.egg),
    ("Tree Nuts and Their Derivatives", Allergen.tree_nuts),
    ("Almond And Almond Products", Allergen.tree_nuts),
    ("Peanuts and their derivates", Allergen.peanuts),
    ("Crustaceans and their derivates", Allergen.shellfish),
    ("Shellfish and its Derivatives", Allergen.shellfish),
    ("Tuna and its Derivatives", Allergen.fish),
    ("Soybean and its Derivatives", Allergen.soy),
    ("Sesame Seeds and their derivates", Allergen.sesame),
    ("Wheat and Their Derivatives", Allergen.wheat),
    ("Cereals and Their Derivatives", Allergen.gluten),
    ("Other Gluten Containing Grain And Gluten Containing Grain Products.", Allergen.gluten),
    ("Coconuts and Their Derivatives", None),
    ("Oats", None),
    ("Celery and its Derivatives", None),
    ("Does Not Contain Declaration Obligatory Allergens", None),
])
def test_allergen_names(name, expected):
    assert allergen_from_name(name) == expected


def test_parse_allergens_description():
    contains, may = parse_allergens_description(
        "Contains Wheat and Their Derivatives,Milk and its derivates. "
        "May contain Peanuts and their derivates.")
    assert contains == {Allergen.wheat, Allergen.milk}
    assert may == {Allergen.peanuts}


def test_aisle_text():
    assert aisle_text([{"description": "BAKING", "number": "12"}]) == "Baking, aisle 12"
    assert aisle_text([{"description": "MEAT"}]) == "Meat"
    assert aisle_text([]) is None


# --- Kroger: auth, retries, cache ----------------------------------------------------------

def test_token_is_reused_until_it_nearly_expires(fixtures_dir):
    session, clock = FakeKrogerSession(fixtures_dir), Clock()
    provider = kroger(session, clock=clock, cache_ttl_s=0)
    provider.search_products("eggs", STORE, 5)
    provider.search_products("bacon", STORE, 5)
    assert session.tokens == 1
    clock.now += 1800 - 30  # inside the one-minute renewal margin
    provider.search_products("eggs", STORE, 5)
    assert session.tokens == 2
    assert provider.calls == 5  # 2 token + 3 product calls


def test_401_gets_a_fresh_token_once(fixtures_dir):
    session = FakeKrogerSession(fixtures_dir, script=[401])
    products = kroger(session).search_products("eggs", STORE, 5)
    assert len(products) == 1 and session.tokens == 2


def test_rate_limit_and_dropped_connection_are_retried(fixtures_dir):
    session = FakeKrogerSession(fixtures_dir, script=[429, "drop"])
    assert len(kroger(session).search_products("eggs", STORE, 5)) == 1
    assert session.count("/products") == 3


def test_persistent_server_error_raises_provider_error(fixtures_dir):
    session = FakeKrogerSession(fixtures_dir, script=[503, 503, 503])
    with pytest.raises(ProviderError, match="failed after 3 tries"):
        kroger(session).search_products("eggs", STORE, 5)


def test_client_error_raises_provider_error(fixtures_dir):
    with pytest.raises(ProviderError, match="HTTP 400"):
        kroger(FakeKrogerSession(fixtures_dir, script=[400])).search_products("eggs", STORE, 5)


def test_searches_are_cached_in_memory(fixtures_dir):
    session = FakeKrogerSession(fixtures_dir)
    provider = kroger(session)
    first = provider.search_products("eggs", STORE, 5)
    assert len(first) == 1
    assert provider.search_products("Eggs", STORE, 5) == first
    assert session.count("/products") == 1


def test_missing_credentials_say_what_to_set():
    with pytest.raises(ProviderError, match="KROGER_CLIENT_ID"):
        KrogerProvider(None, "secret")


# --- Shared helpers ------------------------------------------------------------------------

def test_http_errors_never_include_the_url():
    class LeakySession:
        def request(self, method, url, **kwargs):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    with pytest.raises(ProviderError) as info:
        http_call(LeakySession(), "GET", "https://example.test/request?api_key=SECRET123",
                  what="RedCircle search", attempts=2, backoff_s=0)
    assert "SECRET123" not in str(info.value)
    assert "ConnectionError" in str(info.value)


def test_ttl_cache_expires():
    clock = Clock()
    cache = TTLCache(60, clock)
    cache.set("k", [1])
    assert cache.get("k") == [1]
    clock.now += 61
    assert cache.get("k") is None


def test_haversine_miles():
    # Cincinnati to Columbus, OH is about 99 miles as the crow flies
    assert haversine_miles(39.1031, -84.5120, 39.9612, -82.9988) == pytest.approx(99, abs=3)


class GeoSession:
    def __init__(self, zippopotam_status, nominatim_hits):
        self.zippopotam_status, self.nominatim_hits = zippopotam_status, nominatim_hits

    def request(self, method, url, **kwargs):
        if "zippopotam" in url:
            payload = {"places": [{"latitude": "39.1", "longitude": "-84.5"}]}
            return FakeResponse(self.zippopotam_status, payload)
        return FakeResponse(200, self.nominatim_hits)


def test_zip_centroid_uses_zippopotam_then_nominatim(monkeypatch):
    monkeypatch.setattr(base, "_zip_cache", {})
    assert zip_centroid("45202", GeoSession(200, [])) == (39.1, -84.5)
    monkeypatch.setattr(base, "_zip_cache", {})
    assert zip_centroid("45202", GeoSession(404, [{"lat": "39.2", "lon": "-84.6"}])) == (
        39.2, -84.6)


def test_zip_centroid_fails_clearly(monkeypatch):
    monkeypatch.setattr(base, "_zip_cache", {})
    with pytest.raises(ProviderError, match="could not locate ZIP 00000"):
        zip_centroid("00000", GeoSession(404, []))


def test_make_providers_skips_a_fallback_without_its_key(cfg, monkeypatch, caplog):
    monkeypatch.setenv("KROGER_CLIENT_ID", "id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "secret")
    monkeypatch.delenv("REDCIRCLE_API_KEY", raising=False)
    providers = make_providers(cfg)
    assert [p.name for p in providers] == ["kroger"]
    assert "REDCIRCLE_API_KEY" in caplog.text


def test_make_providers_fails_when_none_can_run(cfg, monkeypatch):
    for name in ("KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET", "REDCIRCLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ProviderError, match="no store provider"):
        make_providers(cfg)


# --- Target (RedCircle) ----------------------------------------------------------------------

class FakeRedCircleSession:
    def __init__(self, fixtures_dir):
        self.payloads = load(fixtures_dir, "req_redcircle.json")
        self.calls = []

    def request(self, method, url, params=None, **kwargs):
        self.calls.append(dict(params))
        if params["type"] == "search":
            key = "probe_search" if params["search_term"] == "milk" else "search"
        else:
            key = "probe_product" if params["tcin"] == "10000001" else "product"
        return FakeResponse(200, self.payloads[key])


def target(session, **kw):
    locations = {"45202": ORIGIN, "45219": (39.13, -84.52)}
    return TargetProvider("key", session=session, backoff_s=0,
                          zip_locator=lambda z: locations[z], **kw)


def test_target_finds_the_nearest_store_from_location_info(fixtures_dir):
    session = FakeRedCircleSession(fixtures_dir)
    [store] = target(session).find_stores("45202", 10, 3)
    assert store.store_id == "target-9999" and store.chain == "TARGET"
    assert store.name == "Target Testville Downtown"
    assert store.address == "2 Sample Ave, Testville, Ohio 45219-1528"
    assert store.distance_miles == pytest.approx(2.1, abs=0.2)
    assert [c["type"] for c in session.calls] == ["search", "product"]
    assert all(c["customer_zipcode"] == "45202" for c in session.calls)


def test_target_store_outside_radius_is_dropped(fixtures_dir):
    assert target(FakeRedCircleSession(fixtures_dir)).find_stores("45202", 1, 3) == []


def test_target_products_get_details_for_the_first_result_only(fixtures_dir):
    session = FakeRedCircleSession(fixtures_dir)
    provider = target(session)
    [store] = provider.find_stores("45202", 10, 3)
    detailed, plain = provider.search_products("gruyere cheese", store, 5)
    assert detailed.barcode == "0012345678905"  # 12-digit UPC-A -> GTIN-13
    assert (detailed.size, detailed.aisle, detailed.in_stock) == ("6oz", "D22", True)
    assert detailed.evidence.ingredients_text.startswith("Pasteurized Milk")
    assert detailed.evidence.categories == ["Grocery", "Dairy", "Cheese"]
    assert plain.barcode is None and plain.evidence is None and plain.size == "8 oz"
    assert provider.credits_used == 4  # store: search + product; ingredient: search + product


def test_target_credit_budget_stops_calls(fixtures_dir, caplog):
    session = FakeRedCircleSession(fixtures_dir)
    provider = target(session, max_credits=3)
    [store] = provider.find_stores("45202", 10, 3)
    [detailed, plain] = provider.search_products("gruyere cheese", store, 5)
    assert detailed.evidence is None  # the detail call would have been credit 4
    assert provider.search_products("bacon", store, 5) == []
    assert len(session.calls) == 3
    assert "budget" in caplog.text


def test_target_unknown_store_is_an_error(fixtures_dir):
    with pytest.raises(ProviderError, match="not found by find_stores"):
        target(FakeRedCircleSession(fixtures_dir)).search_products("milk", STORE, 5)


def test_size_from_title():
    assert size_from_title("Heavy Whipping Cream - 32 fl oz (1qt) - Good & Gather") == "32 fl oz"
    assert size_from_title("Bananas - each") == ""


# --- Live (opt-in) ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
def test_live_kroger_stores_and_products():
    from pantrypath_pipeline.config import load_env, secret

    load_env()
    provider = KrogerProvider(secret("KROGER_CLIENT_ID"), secret("KROGER_CLIENT_SECRET"))
    stores = provider.find_stores("45202", 10, 3)
    assert stores and all(s.distance_miles <= 10 for s in stores)
    products = provider.search_products("eggs", stores[0], 5)
    assert products and all(p.barcode is None or len(p.barcode) == 13 for p in products)
