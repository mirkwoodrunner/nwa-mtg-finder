import httpx
import pytest
from tests.conftest import BASE

def test_health():
    r = httpx.get(f"{BASE}/health", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

def test_debug_returns_json():
    r = httpx.get(f"{BASE}/api/debug?q=Lightning+Bolt", timeout=60)
    assert r.status_code == 200
    data = r.json()
    for store_id in ["finalboss", "gearbv", "gearfv"]:
        assert store_id in data, f"Missing store key: {store_id}"

def test_unknown_store_returns_400():
    r = httpx.get(f"{BASE}/api/search?q=Stock+Up&store=notastore", timeout=10)
    assert r.status_code == 400

def test_missing_query_returns_400():
    r = httpx.get(f"{BASE}/api/search?store=finalboss", timeout=10)
    assert r.status_code == 400
