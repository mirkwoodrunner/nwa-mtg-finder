import os
import time
import threading
import pytest
import httpx

os.environ.setdefault("PORT", "5001")

def _start_flask():
    # Import here so the env var is set first
    from app import app
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)

@pytest.fixture(scope="session", autouse=True)
def flask_server():
    thread = threading.Thread(target=_start_flask, daemon=True)
    thread.start()
    # Poll until the server responds or 15s elapses
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            httpx.get("http://127.0.0.1:5001/health", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    else:
        pytest.fail("Flask server did not start within 15 seconds")
    yield

BASE = "http://127.0.0.1:5001"
PROBE = "Stock Up"
STORES = ["finalboss", "gearbv", "gearfv", "chaos", "xxplo"]
