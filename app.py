"""
NWA MTG Local Store Finder — Backend
"""

import os, re, asyncio, traceback, json, time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests as req
import cloudscraper
from playwright.async_api import async_playwright

app = Flask(__name__, static_folder="static")
CORS(app)

SHOPIFY_STORES = [
    {"id": "finalboss", "name": "Final Boss Games",           "url": "https://finalbossgames.com",               "col": "singles"},
    {"id": "gearbv",    "name": "Gear Gaming — Bentonville",  "url": "https://bentonville.geargamingstore.com",  "col": "mtg-singles-all-products"},
    {"id": "gearfv",    "name": "Gear Gaming — Fayetteville", "url": "https://fayetteville.geargamingstore.com", "col": "mtg-singles-all-products"},
]
TCG_STORES = [
    {"id": "chaos", "name": "Chaos Games",    "url": "https://chaosgamesnwa.tcgplayerpro.com"},
    {"id": "xxplo", "name": "Games Explosion","url": "https://gamesexxplosion.tcgplayerpro.com"},
]
ALL_STORES = SHOPIFY_STORES + TCG_STORES

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SCRAPER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_name(title):
    return re.sub(r"\s*\[.*?\]\s*", "", title).strip()

def extract_set(title):
    m = re.search(r"\[(.+?)\]", title)
    return m.group(1) if m else ""

def parse_shopify_products(products, base_url, filter_query=None):
    if filter_query:
        ql = filter_query.lower()
        products = [p for p in products if ql in (p.get("title") or "").lower()]
    results = []
    for p in products[:15]:
        variants = p.get("variants") or [{}]
        in_stock = [v for v in variants if v.get("available")]
        if not in_stock:
            continue
        price = in_stock[0].get("price")
        url = p.get("url")
        url = f"{base_url}{url}" if url and url.startswith("/") else f"{base_url}/products/{p.get('handle','')}"
        results.append({
            "name":      clean_name(p.get("title") or ""),
            "set":       extract_set(p.get("title") or ""),
            "price":     float(price) if price else None,
            "available": True,
            "url":       url,
        })
    return results

# ── BinderPOS / Shopify — try requests first, Playwright fallback ─────────────

def search_shopify_requests(store, query):
    """
    Fast path: plain HTTP requests with cloudscraper.
    BinderPOS supports /products.json?q= for server-side filtering.
    Falls through to Playwright if no response at all.
    """
    scraper = cloudscraper.create_scraper()
    q = req.utils.quote(query)
    endpoints = [
        # Server-side filtered — most efficient
        (f"{store['url']}/collections/{store['col']}/products.json?q={q}&limit=50", False),
        (f"{store['url']}/collections/all/products.json?q={q}&limit=50", False),
        # Full collection dump with client-side filter
        (f"{store['url']}/collections/{store['col']}/products.json?limit=250", True),
        (f"{store['url']}/collections/all/products.json?limit=250", True),
    ]
    got_any_response = False
    for url, needs_filter in endpoints:
        try:
            r = scraper.get(url, headers=SCRAPER_HEADERS, timeout=12)
            if not r.ok:
                continue
            ct = r.headers.get("content-type", "")
            if "json" not in ct:
                continue
            got_any_response = True
            data = r.json()
            products = data.get("products") or data.get("results") or []
            # Always client-filter to ensure relevance
            parsed = parse_shopify_products(products, store["url"], query)
            if parsed is not None:
                return parsed, None
        except Exception:
            continue
    if got_any_response:
        return [], None   # responded but 0 results — not in stock
    return None, "requests failed"  # None = try Playwright

async def search_shopify_playwright(store, query, page):
    """Playwright fallback for stores that block plain requests."""
    q = req.utils.quote(query)
    endpoints = [
        (f"{store['url']}/search?q={q}&type=product&view=json", False),
        (f"{store['url']}/search.json?q={q}&type=product&limit=20", False),
        (f"{store['url']}/collections/{store['col']}/products.json?limit=250", True),
        (f"{store['url']}/collections/all/products.json?limit=250", True),
    ]
    for url, needs_filter in endpoints:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if not resp or not resp.ok:
                continue
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                continue
            data = await resp.json()
            products = data.get("products") or data.get("results") or []
            parsed = parse_shopify_products(products, store["url"], query if needs_filter else None)
            if parsed is not None:
                return parsed, None
        except Exception:
            continue
    return [], "No results found"

# ── TCGPlayer Pro — intercept internal API calls ──────────────────────────────

async def search_tcgpro(store, query, page):
    search_url = (f"{store['url']}/search/products"
                  f"?productLineName=Magic%3A+The+Gathering"
                  f"&q={req.utils.quote(query)}")

    # Only intercept calls from THIS store's domain
    store_domain = store["url"].replace("https://", "").split("/")[0]
    intercepted = []

    async def on_response(response):
        url = response.url
        # Must be from this store, must be JSON, must look like a search/product endpoint
        if store_domain not in url:
            return
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        if not any(k in url.lower() for k in ["search", "product", "catalog"]):
            return
        try:
            data = await response.json()
            # Must contain card-like items — look for arrays with name/price fields
            candidates = []
            if isinstance(data, list):
                candidates = data
            elif isinstance(data, dict):
                for key in ["results", "products", "items", "data", "cards"]:
                    val = data.get(key)
                    if isinstance(val, list) and val:
                        candidates = val
                        break
            # Validate: items must have a name-like field
            if candidates and isinstance(candidates[0], dict):
                has_name = any(k in candidates[0] for k in
                               ["name","productName","cleanName","title"])
                if has_name:
                    intercepted.append(candidates)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(search_url, wait_until="networkidle", timeout=25000)
        await page.wait_for_timeout(2000)
    except Exception:
        pass
    page.remove_listener("response", on_response)

    results = []
    for item_list in intercepted:
        for item in item_list[:15]:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or item.get("productName") or
                    item.get("cleanName") or item.get("title") or "")
            if not name:
                continue

            # Price — check many possible field names and nested structures
            price = None
            for pk in ["marketPrice", "lowPrice", "price", "lowestPrice",
                       "minPrice", "retailPrice", "salePrice"]:
                v = item.get(pk)
                if v is not None:
                    try:
                        price = float(str(v).replace("$","").replace(",",""))
                        if price > 0:
                            break
                    except Exception:
                        pass
            # Also check nested pricing objects
            if price is None:
                for pricing_key in ["pricing", "prices", "priceData"]:
                    nested = item.get(pricing_key)
                    if isinstance(nested, dict):
                        for pk in ["market","low","mid","direct"]:
                            v = nested.get(pk)
                            if v:
                                try:
                                    price = float(str(v).replace("$","").replace(",",""))
                                    if price > 0:
                                        break
                                except Exception:
                                    pass
                    if price:
                        break

            # Stock
            qty = (item.get("quantity") or item.get("qty") or
                   item.get("stock") or item.get("inventory") or 1)
            try:
                available = int(str(qty).split(".")[0]) > 0
            except Exception:
                available = True
            if not available:
                continue

            slug = (item.get("slug") or item.get("handle") or
                    item.get("urlKey") or item.get("url") or "")
            if slug and not slug.startswith("http"):
                item_url = f"{store['url']}/product/{slug.lstrip('/')}"
            elif slug:
                item_url = slug
            else:
                item_url = search_url

            set_name = (item.get("setName") or item.get("groupName") or
                        item.get("expansion") or extract_set(name))

            results.append({
                "name":      clean_name(name),
                "set":       set_name,
                "price":     price,
                "available": True,
                "url":       item_url,
            })
        if results:
            break

    if not results:
        return [], "No results found"
    return results, None

# ── Run all store searches ────────────────────────────────────────────────────

async def search_all_stores(query):
    store_results = {}

    # ── Step 1: Try all Shopify stores with plain requests (fast, no browser) ──
    shopify_needs_playwright = []
    for store in SHOPIFY_STORES:
        try:
            res, err = search_shopify_requests(store, query)
            if res is None:
                # requests path failed entirely — queue for Playwright
                shopify_needs_playwright.append(store)
                store_results[store["id"]] = ([], "queued")
            else:
                store_results[store["id"]] = (res, err if not res else None)
        except Exception as e:
            shopify_needs_playwright.append(store)
            store_results[store["id"]] = ([], "queued")

    # ── Step 2: Playwright for TCG stores + any Shopify stores that failed ──
    needs_playwright = shopify_needs_playwright + TCG_STORES
    if needs_playwright:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
            )
            context = await browser.new_context(user_agent=UA)
            page = await context.new_page()

            for store in needs_playwright:
                try:
                    if store in SHOPIFY_STORES:
                        fn = search_shopify_playwright(store, query, page)
                    else:
                        fn = search_tcgpro(store, query, page)
                    res, err = await asyncio.wait_for(fn, timeout=28)
                    store_results[store["id"]] = (res, err if not res else None)
                except asyncio.TimeoutError:
                    store_results[store["id"]] = ([], "Timed out")
                except Exception as e:
                    store_results[store["id"]] = ([], str(e)[:120])

            await browser.close()

    output = []
    for store in ALL_STORES:
        res, err = store_results.get(store["id"], ([], "Not searched"))
        entry = {
            "id": store["id"], "name": store["name"], "url": store["url"],
            "results": res, "error": err,
        }
        if store in TCG_STORES:
            entry["search_url"] = (
                f"{store['url']}/search/products"
                f"?productLineName=Magic%3A+The+Gathering"
                f"&q={req.utils.quote(query)}"
            )
        output.append(entry)
    return output

# ── Moxfield ──────────────────────────────────────────────────────────────────

def error_mox(username, card_name, msg):
    return {
        "username": username, "found_in": [], "unknown": [], "total_decks": 0,
        "error": msg,
        "profile_url": f"https://www.moxfield.com/users/{username}",
        "search_url":  f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
    }

async def search_moxfield_async(username, card_name):
    """
    Use Playwright to intercept the XHR requests Moxfield's own page makes.
    This captures the authenticated API calls with proper tokens automatically.
    1. Load moxfield.com/users/{username} — intercept the decks XHR
    2. For each deck load moxfield.com/decks/{id} — intercept the cards XHR
    """
    card_lower = card_name.lower()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--single-process"],
        )
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()

        # ── Step 1: Load user profile and intercept the decks API call ──
        deck_list = []
        deck_list_event = asyncio.Event()

        async def on_decks_response(response):
            if ("moxfield.com" in response.url and "/decks" in response.url
                    and response.status == 200):
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        data = await response.json()
                        items = data.get("data", [])
                        if items:
                            deck_list.extend(items)
                            deck_list_event.set()
                    except Exception:
                        pass

        page.on("response", on_decks_response)
        try:
            await page.goto(f"https://www.moxfield.com/users/{username}",
                            wait_until="networkidle", timeout=20000)
            # Give XHRs a moment to complete
            try:
                await asyncio.wait_for(deck_list_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        except Exception:
            pass
        page.remove_listener("response", on_decks_response)

        if not deck_list:
            await browser.close()
            return error_mox(username, card_name,
                f"Could not load decks for '{username}'. "
                "Check the username is correct and profile is public.")

        # ── Step 2: For each deck, load the deck page and intercept the cards XHR ──
        found_in = []
        for deck in deck_list:
            public_id = deck.get("publicId") or deck.get("id", "")
            if not public_id:
                continue
            deck_info = {
                "name":   deck.get("name", "Unnamed"),
                "format": deck.get("format", ""),
                "url":    f"https://www.moxfield.com/decks/{public_id}",
            }

            card_data = {}
            cards_event = asyncio.Event()

            async def on_cards_response(response):
                if ("moxfield.com" in response.url
                        and public_id in response.url
                        and response.status == 200):
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        try:
                            data = await response.json()
                            # Deck detail has mainboard/commanders etc as dicts
                            for zone in ["mainboard","commanders","sideboard","maybeboard"]:
                                if isinstance(data.get(zone), dict) and data[zone]:
                                    card_data.update(data)
                                    cards_event.set()
                                    break
                        except Exception:
                            pass

            page.on("response", on_cards_response)
            try:
                await page.goto(f"https://www.moxfield.com/decks/{public_id}",
                                wait_until="networkidle", timeout=20000)
                try:
                    await asyncio.wait_for(cards_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            except Exception:
                pass
            page.remove_listener("response", on_cards_response)

            # Check if card is in any zone
            for zone in ["mainboard", "commanders", "sideboard", "maybeboard", "companion"]:
                zone_cards = card_data.get(zone, {})
                if isinstance(zone_cards, dict):
                    for card_key in zone_cards:
                        if card_lower in card_key.lower():
                            found_in.append(deck_info)
                            break
                    else:
                        continue
                    break

        await browser.close()

    return {
        "username":    username,
        "found_in":    found_in,
        "unknown":     [],
        "total_decks": len(deck_list),
        "profile_url": f"https://www.moxfield.com/users/{username}",
        "search_url":  f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
    }

def search_moxfield(username, card_name):
    """Sync wrapper around the async Playwright moxfield search."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(search_moxfield_async(username, card_name))
    except Exception as e:
        return error_mox(username, card_name, f"Moxfield error: {e}")
    finally:
        loop.close()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stores = loop.run_until_complete(search_all_stores(query))
        loop.close()
        return jsonify({"query": query, "stores": stores})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/moxfield")
def api_moxfield():
    username = request.args.get("username", "").strip()
    card     = request.args.get("card", "").strip()
    if not username or not card:
        return jsonify({"error": "username and card required"}), 400
    try:
        # Moxfield search is synchronous (cloudscraper), run in thread pool
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as ex:
            future = ex.submit(search_moxfield, username, card)
            result = future.result(timeout=120)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/debug")
def api_debug():
    """Smoke test — checks Final Boss (cloudscraper) and Moxfield (Playwright)."""
    out = {}

    # Test 1: Final Boss with cloudscraper - try multiple endpoints
    scraper = cloudscraper.create_scraper()
    for path, label in [
        ("/search.json?q=plains&type=product&limit=5", "search.json"),
        ("/collections/singles/products.json?limit=5", "collection"),
    ]:
        try:
            r = scraper.get(f"https://finalbossgames.com{path}",
                            headers=SCRAPER_HEADERS, timeout=12)
            ct = r.headers.get("content-type", "")
            if r.ok and "json" in ct:
                data = r.json()
                products = data.get("products") or data.get("results") or []
                out[f"finalboss_{label}"] = {"status": r.status_code, "products": len(products),
                    "first": products[0].get("title","") if products else "none"}
            else:
                out[f"finalboss_{label}"] = {"status": r.status_code, "content_type": ct}
        except Exception as e:
            out[f"finalboss_{label}"] = {"error": str(e)}

    # Test 2: Moxfield XHR interception
    async def test_mox():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-dev-shm-usage","--single-process"],
            )
            context = await browser.new_context(user_agent=UA)
            page = await context.new_page()
            intercepted = {}
            async def on_resp(response):
                if "moxfield.com" in response.url and "/decks" in response.url and response.status == 200:
                    ct = response.headers.get("content-type","")
                    if "json" in ct:
                        try:
                            data = await response.json()
                            items = data.get("data",[])
                            if items:
                                intercepted["decks"] = len(items)
                                intercepted["first"] = items[0].get("name","")
                                intercepted["url"] = response.url
                        except Exception as e:
                            intercepted["parse_error"] = str(e)
            page.on("response", on_resp)
            try:
                await page.goto("https://www.moxfield.com/users/mirkwoodrunner",
                                wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                intercepted["nav_error"] = str(e)
            await browser.close()
            return intercepted if intercepted else {"note": "no moxfield XHR intercepted"}

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        out["moxfield_xhr"] = loop.run_until_complete(test_mox())
        loop.close()
    except Exception as e:
        out["moxfield_xhr"] = {"error": str(e)}

    return jsonify(out)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✦ NWA MTG Finder — http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
