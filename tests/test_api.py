import pytest
import httpx
from tests.conftest import BASE, PROBE, STORES

SHOPIFY_STORES = ["finalboss", "gearbv", "gearfv"]
TCG_STORES = ["chaos", "xxplo"]

def _search(store_id, timeout=90):
    return httpx.get(
        f"{BASE}/api/search",
        params={"q": PROBE, "store": store_id},
        timeout=timeout,
    )

# ── Response shape ─────────────────────────────────────────────────────────────
# Every store must return a well-formed response regardless of inventory result.

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_response_is_200(store_id):
    r = _search(store_id)
    assert r.status_code == 200, f"[{store_id}] HTTP {r.status_code}"

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_response_has_required_keys(store_id):
    data = _search(store_id).json()
    for key in ("query", "store", "results", "search_url"):
        assert key in data, f"[{store_id}] Missing key: {key}"

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_results_is_list(store_id):
    data = _search(store_id).json()
    assert isinstance(data["results"], list), f"[{store_id}] results is not a list"

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_query_echoed_correctly(store_id):
    data = _search(store_id).json()
    assert data["query"] == PROBE, f"[{store_id}] query not echoed"

# ── Inventory results ──────────────────────────────────────────────────────────
# Each store has confirmed copies of Stock Up. These tests FAIL if scraping breaks.

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_at_least_one_result_found(store_id):
    data = _search(store_id).json()
    results = data["results"]
    assert len(results) > 0, (
        f"[{store_id}] Zero results for '{PROBE}'. "
        f"error={data.get('error')} search_url={data.get('search_url')}"
    )

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_result_items_have_required_keys(store_id):
    data = _search(store_id).json()
    for i, item in enumerate(data["results"]):
        for key in ("name", "price", "available", "url"):
            assert key in item, f"[{store_id}] result[{i}] missing key: {key}"

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_result_names_match_probe(store_id):
    data = _search(store_id).json()
    for item in data["results"]:
        assert "stock up" in item["name"].lower(), (
            f"[{store_id}] Result name '{item['name']}' does not match probe '{PROBE}'"
        )

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_result_prices_are_positive_floats(store_id):
    data = _search(store_id).json()
    for item in data["results"]:
        if item["price"] is not None:
            assert isinstance(item["price"], (int, float)), \
                f"[{store_id}] price is not numeric: {item['price']}"
            assert item["price"] > 0, \
                f"[{store_id}] price is zero or negative: {item['price']}"

@pytest.mark.parametrize("store_id", STORES)
@pytest.mark.timeout(90)
def test_result_urls_are_absolute(store_id):
    data = _search(store_id).json()
    for item in data["results"]:
        assert item["url"].startswith("http"), \
            f"[{store_id}] URL is not absolute: {item['url']}"
