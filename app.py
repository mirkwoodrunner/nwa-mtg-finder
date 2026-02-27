"""
NWA MTG Local Store Finder — Backend
Single Playwright browser, sequential store searches, generous timeouts.
"""

import os, re, asyncio, traceback, json
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
        url = f"{base_url}{url}" if url else f"{base_url}/products/{p.get('handle','')}"
        results.append({
            "name":      clean_name(p.get("title") or ""),
            "set":       extract_set(p.get("title") or ""),
            "price":     float(price) if price else None,
            "available": True,
            "url":       url,
        })
    return results

# ── Per-store search functions ────────────────────────────────────────────────

async def search_shopify(store, query, page):
    q = req.utils.quote(query)
    endpoints = [
        (f"{store['url']}/search?q={q}&type=product&view=json", False),
        (f"{store['url']}/search.json?q={q}&type=product&limit=20", False),
        (f"{store['url']}/collections/{store['col']}/products.json?limit=250", True),
        (f"{store['url']}/collections/all/products.json?limit=250", True),
    ]
    for url, needs_filter in endpoints:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if not resp or not resp.ok:
                continue
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                continue
            data = await resp.json()
            products = data.get("products") or data.get("results") or []
            parsed = parse_shopify_products(products, store["url"], query if needs_filter else None)
            if parsed:
                return parsed, None
        except Exception as e:
            continue
    return [], "No results found"


async def search_tcgpro(store, query, page):
    search_url = (f"{store['url']}/search/products"
                  f"?productLineName=Magic%3A+The+Gathering"
                  f"&q={req.utils.quote(query)}")
    intercepted = []

    async def on_response(response):
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        url = response.url
        if any(k in url for k in ["search", "product", "catalog", "api"]):
            try:
                data = await response.json()
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    intercepted.append(data)
                elif isinstance(data, dict):
                    for key in ["results", "products", "items", "data", "cards"]:
                        val = data.get(key)
                        if isinstance(val, list) and val:
                            intercepted.append(val)
                            break
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
            price = None
            for pk in ["marketPrice", "lowPrice", "price", "lowestPrice", "minPrice"]:
                v = item.get(pk)
                if v is not None:
                    try:
                        price = float(str(v).replace("$", "").replace(",", ""))
                        break
                    except Exception:
                        pass
            qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
            try:
                available = int(qty) > 0
            except Exception:
                available = True
            if not available:
                continue
            slug = item.get("slug") or item.get("handle") or item.get("urlKey") or ""
            item_url = f"{store['url']}/product/{slug}" if slug else search_url
            set_name = (item.get("setName") or item.get("groupName") or
                        item.get("expansion") or extract_set(name))
            results.append({
                "name": clean_name(name), "set": set_name,
                "price": price, "available": True, "url": item_url,
            })
        if results:
            break

    # DOM fallback
    if not results:
        try:
            items = await page.evaluate("""() => {
                const out = [];
                const sels = ['[class*="product-card"]','[class*="ProductCard"]',
                    '[class*="search-result"]','.product','[data-testid*="product"]'];
                let cards = [];
                for (const s of sels) { cards = [...document.querySelectorAll(s)]; if (cards.length) break; }
                for (const card of cards.slice(0,12)) {
                    const text = card.innerText || '';
                    if (/out.of.stock|sold.out/i.test(text)) continue;
                    const link = card.querySelector('a[href]');
                    const pm = text.match(/\\$([0-9]+\\.[0-9]{2})/);
                    const lines = text.split('\\n').map(s=>s.trim()).filter(Boolean);
                    if (lines.length) out.push({ name: lines[0], price: pm ? pm[1] : null,
                        href: link ? link.href : null });
                }
                return out;
            }""")
            for item in items:
                name = item.get("name", "").strip()
                if len(name) < 2:
                    continue
                price_str = item.get("price")
                results.append({
                    "name": name, "set": "",
                    "price": float(price_str) if price_str else None,
                    "available": True, "url": item.get("href") or search_url,
                })
        except Exception:
            pass

    return (results, None) if results else ([], "No results found")


async def search_all_stores(query):
    """One browser, one context, one page — reused across all stores sequentially."""
    store_results = {s["id"]: ([], "Not searched") for s in ALL_STORES}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu",
                  "--single-process"],
        )
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()

        for store in SHOPIFY_STORES:
            try:
                res, err = await asyncio.wait_for(
                    search_shopify(store, query, page), timeout=25
                )
                store_results[store["id"]] = (res, err)
            except asyncio.TimeoutError:
                store_results[store["id"]] = ([], "Timed out")
            except Exception as e:
                store_results[store["id"]] = ([], str(e)[:120])

        for store in TCG_STORES:
            try:
                res, err = await asyncio.wait_for(
                    search_tcgpro(store, query, page), timeout=30
                )
                store_results[store["id"]] = (res, err)
            except asyncio.TimeoutError:
                store_results[store["id"]] = ([], "Timed out")
            except Exception as e:
                store_results[store["id"]] = ([], str(e)[:120])

        await browser.close()

    output = []
    for store in ALL_STORES:
        res, err = store_results[store["id"]]
        entry = {
            "id":      store["id"],
            "name":    store["name"],
            "url":     store["url"],
            "results": res,
            "error":   err if not res else None,
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

async def search_moxfield(username, card_name):
    api_url = (f"https://api2.moxfield.com/v2/users/{username}/decks"
               f"?pageNumber=1&pageSize=100&sortType=updated&sortDirection=Descending")
    card_lower = card_name.lower()
    decks = []

    # Try cloudscraper first
    try:
        scraper = cloudscraper.create_scraper()
        r = scraper.get(api_url, headers={
            "Accept": "application/json",
            "Referer": "https://www.moxfield.com/",
            "Origin": "https://www.moxfield.com",
        }, timeout=15)
        if r.ok:
            decks = r.json().get("data", [])
    except Exception:
        pass

    # Playwright fallback — navigate directly to the API URL
    if not decks:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-dev-shm-usage", "--single-process"],
                )
                context = await browser.new_context(user_agent=UA)
                page = await context.new_page()
                resp = await page.goto(api_url, wait_until="domcontentloaded", timeout=20000)
                if resp and resp.ok:
                    try:
                        data = await resp.json()
                        decks = data.get("data", [])
                    except Exception:
                        body = await page.inner_text("body")
                        data = json.loads(body)
                        decks = data.get("data", [])
                await browser.close()
        except Exception as e:
            return {
                "username": username, "found_in": [], "total_decks": 0,
                "error": f"Could not reach Moxfield: {e}",
                "profile_url": f"https://www.moxfield.com/users/{username}",
                "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
            }

    if not decks:
        return {
            "username": username, "found_in": [], "unknown": [], "total_decks": 0,
            "error": f"No public decks found for '{username}'.",
            "profile_url": f"https://www.moxfield.com/users/{username}",
            "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
        }

    found_in, unknown = [], []
    for deck in decks:
        deck_url = f"https://www.moxfield.com/decks/{deck.get('publicId', deck.get('id',''))}"
        deck_info = {"name": deck.get("name","Unnamed"), "format": deck.get("format",""), "url": deck_url}
        found, has_data = False, False
        for zone in ["mainboard","commanders","sideboard","maybeboard","companion"]:
            zc = deck.get(zone, {})
            if isinstance(zc, dict) and zc:
                has_data = True
                if any(card_lower in k.lower() for k in zc):
                    found = True
                    break
        if has_data:
            if found:
                found_in.append(deck_info)
        else:
            unknown.append(deck_info)

    return {
        "username": username, "found_in": found_in, "unknown": unknown,
        "total_decks": len(decks),
        "profile_url": f"https://www.moxfield.com/users/{username}",
        "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
    }

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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(search_moxfield(username, card))
        loop.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/debug")
def api_debug():
    """Quick smoke test — searches one store only to verify Playwright works."""
    try:
        async def test():
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox","--disable-setuid-sandbox",
                          "--disable-dev-shm-usage","--single-process"],
                )
                context = await browser.new_context(user_agent=UA)
                page = await context.new_page()
                resp = await page.goto(
                    "https://finalbossgames.com/search?q=lightning+bolt&type=product&view=json",
                    wait_until="domcontentloaded", timeout=20000
                )
                status = resp.status if resp else "no response"
                ct = resp.headers.get("content-type","") if resp else ""
                body_preview = ""
                if resp and resp.ok and "json" in ct:
                    data = await resp.json()
                    products = data.get("products") or data.get("results") or []
                    body_preview = f"{len(products)} products found"
                await browser.close()
                return {"status": status, "content_type": ct, "result": body_preview}
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(test())
        loop.close()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✦ NWA MTG Finder — http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
