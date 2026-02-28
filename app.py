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

# ── BinderPOS / Shopify ───────────────────────────────────────────────────────

async def search_shopify(store, query):
    """
    Search a BinderPOS/Shopify store using async requests (aiohttp-style via
    asyncio executor so we can run all stores in parallel).
    Strategy: search endpoint first (1 request), then paginate with early exit.
    """
    loop = asyncio.get_event_loop()
    scraper = cloudscraper.create_scraper()
    q = req.utils.quote(query)

    def _fetch(url):
        try:
            r = scraper.get(url, headers=SCRAPER_HEADERS, timeout=10)
            if not r.ok: return None
            if "json" not in r.headers.get("content-type","").lower(): return None
            return r.json()
        except Exception:
            return None

    # 1. Search endpoint — single request, server-filtered
    for path in [f"/search?q={q}&type=product&view=json", f"/search?q={q}&view=json"]:
        data = await loop.run_in_executor(None, _fetch, store["url"] + path)
        if data:
            products = data.get("products") or data.get("results") or []
            if isinstance(products, list):
                parsed = parse_shopify_products(products, store["url"], query)
                if parsed is not None:
                    return parsed, None

    # 2. Paginate collection — exit as soon as match found, cap at 3 pages
    for collection in [store["col"], "all"]:
        for page_num in range(1, 4):
            url = f"{store['url']}/collections/{collection}/products.json?limit=250&page={page_num}"
            data = await loop.run_in_executor(None, _fetch, url)
            if not data:
                break
            products = data.get("products", [])
            if not products:
                break
            parsed = parse_shopify_products(products, store["url"], query)
            if parsed:
                return parsed, None
            if len(products) < 250:
                return [], None  # last page, no match
        else:
            continue
        break

    return [], None

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

# ── Run all store searches in parallel ───────────────────────────────────────

async def search_store(store, query, browser):
    """Search one store — Shopify via async requests, TCG via Playwright page."""
    try:
        if store in SHOPIFY_STORES:
            return await asyncio.wait_for(search_shopify(store, query), timeout=20)
        else:
            context = await browser.new_context(user_agent=UA)
            page = await context.new_page()
            try:
                return await asyncio.wait_for(search_tcgpro(store, query, page), timeout=25)
            finally:
                await page.close()
                await context.close()
    except asyncio.TimeoutError:
        return [], "Timed out"
    except Exception as e:
        return [], str(e)[:120]


async def search_all_stores(query):
    """
    Run all 5 store searches concurrently.
    Shopify stores use async HTTP (no browser needed).
    TCG stores use Playwright pages — each gets its own page but shares one browser.
    Total wall time = slowest single store, not sum of all stores.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        )
        tasks = [search_store(store, query, browser) for store in ALL_STORES]
        results = await asyncio.gather(*tasks)
        await browser.close()

    output = []
    for store, (res, err) in zip(ALL_STORES, results):
        entry = {
            "id": store["id"], "name": store["name"], "url": store["url"],
            "results": res or [], "error": err if not res else None,
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

    # Test 1: Final Boss — test search + paginated collection
    try:
        fb_store = {"url": "https://finalbossgames.com", "col": "singles"}
        results, err = search_shopify_requests(fb_store, "plains")
        out["finalboss_search"] = {
            "matches": len(results) if results else 0,
            "error": err,
            "first": results[0]["name"] if results else "none",
        }
    except Exception as e:
        out["finalboss_search"] = {"error": str(e)}

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
