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

# Moxfield is handled entirely client-side in the frontend.
# Render's data-center IP gets 403 from Moxfield's Cloudflare regardless of approach.
# The browser (residential IP) calls api.moxfield.com directly with no issues.

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

    # Test 2: Moxfield public search API (no auth needed)
    try:
        scraper2 = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )
        scraper2.headers.update({
            "Accept": "application/json",
            "Referer": "https://www.moxfield.com/",
            "Origin": "https://www.moxfield.com",
        })
        # Search for Plains in mirkwoodrunner's decks
        r2 = scraper2.get(
            "https://api.moxfield.com/v2/decks/search"
            "?pageNumber=1&pageSize=5&q=Plains&authorUsernames=mirkwoodrunner",
            timeout=15
        )
        if r2.ok:
            data2 = r2.json()
            results = data2.get("data", [])
            out["moxfield_search"] = {
                "status": r2.status_code,
                "total": data2.get("totalResults", len(results)),
                "decks": len(results),
                "first": results[0].get("name","") if results else "none",
            }
        else:
            out["moxfield_search"] = {"status": r2.status_code, "error": "not ok"}
    except Exception as e:
        out["moxfield_search"] = {"error": str(e)}

    return jsonify(out)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✦ NWA MTG Finder — http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
