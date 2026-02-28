"""
NWA MTG Local Store Finder — Backend
"""

import os, re, asyncio, traceback, json, time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests as req
import cloudscraper
from playwright.async_api import async_playwright
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    async def stealth_async(page): pass

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

def shopify_fetch_all_products(scraper, base_url, collection):
    """
    Paginate through ALL products in a BinderPOS collection.
    Returns flat list of all product dicts.
    """
    all_products = []
    page = 1
    while True:
        url = f"{base_url}/collections/{collection}/products.json?limit=250&page={page}"
        try:
            r = scraper.get(url, headers=SCRAPER_HEADERS, timeout=12)
            if not r.ok:
                break
            ct = r.headers.get("content-type", "")
            if "json" not in ct:
                break
            products = r.json().get("products", [])
            if not products:
                break
            all_products.extend(products)
            # If fewer than 250 returned, we've hit the last page
            if len(products) < 250:
                break
            page += 1
            if page > 20:  # safety cap: max 5000 products
                break
        except Exception:
            break
    return all_products


def search_shopify_requests(store, query):
    """
    Fast path: plain HTTP requests with cloudscraper.
    Paginates through the full collection and filters client-side.
    Falls through to Playwright if no HTTP response at all.
    """
    scraper = cloudscraper.create_scraper()
    got_any_response = False

    for collection in [store["col"], "all"]:
        try:
            # Quick probe: fetch page 1 to confirm endpoint works
            probe_url = f"{store['url']}/collections/{collection}/products.json?limit=250&page=1"
            r = scraper.get(probe_url, headers=SCRAPER_HEADERS, timeout=12)
            if not r.ok or "json" not in r.headers.get("content-type", ""):
                continue
            got_any_response = True
            first_page = r.json().get("products", [])
            if first_page is None:
                continue

            # Check first page for matches
            parsed = parse_shopify_products(first_page, store["url"], query)
            if parsed:
                return parsed, None

            # If first page had 250 items, there are more — paginate
            if len(first_page) == 250:
                rest = shopify_fetch_all_products(scraper, store["url"], collection)
                all_products = first_page + rest
                parsed = parse_shopify_products(all_products, store["url"], query)
                return parsed, None

            # First page had < 250 and no matches — card not in this collection
            return [], None
        except Exception:
            continue

    if got_any_response:
        return [], None
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

async def mox_get(page, url):
    """
    Navigate a stealth browser page directly to a Moxfield API URL.
    No CSP, no origin restrictions — just a browser GETting a JSON endpoint.
    """
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        if not resp:
            return None, 0
        if not resp.ok:
            return None, resp.status
        # The response body is raw JSON rendered as text in the browser
        body = await page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        return data, resp.status
    except Exception:
        return None, 0


async def search_moxfield_async(username, card_name):
    """
    Navigate a stealth browser directly to api.moxfield.com endpoints.
    No page.evaluate, no fetch() — just page.goto() to JSON URLs.
    Works because: stealth hides headless, and navigating directly to the
    API avoids CSP entirely (CSP only applies within a page context).
    """
    card_lower = card_name.lower()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--single-process"],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.moxfield.com/",
                "Origin": "https://www.moxfield.com",
            }
        )
        page = await context.new_page()
        await stealth_async(page)

        # Warm up: visit moxfield.com to get any cookies/session tokens
        try:
            await page.goto("https://www.moxfield.com", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(800)
        except Exception:
            pass

        # ── Step 1: Fetch deck list ──
        list_url = (f"https://api.moxfield.com/v2/users/{username}/decks"
                    f"?pageNumber=1&pageSize=100&sortType=updated&sortDirection=Descending")
        deck_data, status = await mox_get(page, list_url)

        if not deck_data:
            await browser.close()
            return error_mox(username, card_name,
                f"Moxfield returned HTTP {status} for '{username}'. "
                "Check the username is correct and the profile is Public.")

        deck_list = deck_data.get("data", [])
        if not deck_list:
            await browser.close()
            return error_mox(username, card_name,
                f"No public decks found for '{username}'.")

        # ── Step 2: Check each deck ──
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
            detail, _ = await mox_get(
                page, f"https://api.moxfield.com/v2/decks/all/{public_id}"
            )
            if not detail:
                continue
            for zone in ["mainboard", "commanders", "sideboard", "maybeboard", "companion"]:
                zone_cards = detail.get(zone, {})
                if isinstance(zone_cards, dict):
                    for card_key in zone_cards:
                        if card_lower in card_key.lower():
                            found_in.append(deck_info)
                            break
                    else:
                        continue
                    break
            await asyncio.sleep(0.1)

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

    # Test 1: Final Boss — test full pagination
    scraper = cloudscraper.create_scraper()
    try:
        all_products = shopify_fetch_all_products(scraper, "https://finalbossgames.com", "singles")
        parsed = parse_shopify_products(all_products, "https://finalbossgames.com", "plains")
        out["finalboss_paginated"] = {
            "total_products": len(all_products),
            "plains_matches": len(parsed),
            "first_match": parsed[0]["name"] if parsed else "none",
        }
    except Exception as e:
        out["finalboss_paginated"] = {"error": str(e)}

    out["stealth_available"] = HAS_STEALTH

    # Test 2: Moxfield via direct stealth page.goto to API URL
    async def test_mox():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-dev-shm-usage","--single-process"],
            )
            context = await browser.new_context(
                user_agent=UA, viewport={"width":1280,"height":800},
                extra_http_headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.moxfield.com/",
                    "Origin": "https://www.moxfield.com",
                }
            )
            page = await context.new_page()
            await stealth_async(page)
            result = {}
            try:
                await page.goto("https://www.moxfield.com", wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(800)
                url = "https://api.moxfield.com/v2/users/mirkwoodrunner/decks?pageNumber=1&pageSize=5"
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                status = resp.status if resp else 0
                if resp and resp.ok:
                    body = await page.evaluate("() => document.body.innerText")
                    data = json.loads(body)
                    decks = data.get("data", [])
                    result = {"status": status, "decks": len(decks),
                              "first": decks[0].get("name","") if decks else "none"}
                else:
                    result = {"status": status, "error": "not ok"}
            except Exception as e:
                result = {"error": str(e)}
            await browser.close()
            return result

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        out["moxfield_direct"] = loop.run_until_complete(test_mox())
        loop.close()
    except Exception as e:
        out["moxfield_direct"] = {"error": str(e)}

    return jsonify(out)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✦ NWA MTG Finder — http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
