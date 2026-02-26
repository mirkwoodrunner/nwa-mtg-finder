"""
NWA MTG Local Store Finder — Backend
Uses Playwright for all stores (most reliable on Render)
"""

import os, re, asyncio, json
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests as req
import cloudscraper
from playwright.async_api import async_playwright

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Store config ──────────────────────────────────────────────────────────────

SHOPIFY_STORES = [
    {"id": "finalboss", "name": "Final Boss Games",           "url": "https://finalbossgames.com",               "col": "singles"},
    {"id": "gearbv",    "name": "Gear Gaming — Bentonville",  "url": "https://bentonville.geargamingstore.com",  "col": "mtg-singles-all-products"},
    {"id": "gearfv",    "name": "Gear Gaming — Fayetteville", "url": "https://fayetteville.geargamingstore.com", "col": "mtg-singles-all-products"},
]

TCG_STORES = [
    {"id": "chaos", "name": "Chaos Games NWA", "url": "https://chaosgamesnwa.tcgplayerpro.com"},
    {"id": "xxplo", "name": "Games Xxplosion",  "url": "https://gamesexxplosion.tcgplayerpro.com"},
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_name(title):
    return re.sub(r"\s*\[.*?\]\s*", "", title).strip()

def extract_set(title):
    m = re.search(r"\[(.+?)\]", title)
    return m.group(1) if m else ""

def parse_shopify_products(products, base_url, filter_query=None):
    """Parse Shopify/BinderPOS product list, in-stock only."""
    if filter_query:
        ql = filter_query.lower()
        products = [p for p in products if ql in (p.get("title") or "").lower()]
    results = []
    for p in products[:12]:
        variants = p.get("variants") or [{}]
        # Only include in-stock variants
        in_stock_variants = [v for v in variants if v.get("available")]
        if not in_stock_variants:
            continue  # skip out-of-stock products entirely
        price = in_stock_variants[0].get("price")
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

# ── Playwright browser launcher ───────────────────────────────────────────────

async def make_browser(playwright):
    return await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-dev-shm-usage", "--disable-gpu",
              "--single-process"],
    )

# ── BinderPOS / Shopify search via Playwright ─────────────────────────────────

async def search_shopify_pw(store, query, browser):
    """
    Use Playwright to search a BinderPOS/Shopify store.
    Intercepts the JSON API response directly — much more reliable than DOM scraping.
    """
    q_enc = req.utils.quote(query)
    
    # Endpoints to try in order
    endpoints = [
        f"{store['url']}/search?q={q_enc}&type=product&view=json",
        f"{store['url']}/search.json?q={q_enc}&type=product&limit=20",
        f"{store['url']}/collections/{store['col']}/products.json?limit=250",
        f"{store['url']}/collections/all/products.json?limit=250",
    ]

    context = await browser.new_context(user_agent=UA)
    page = await context.new_page()

    for i, url in enumerate(endpoints):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if not response or not response.ok:
                continue
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                continue
            data = await response.json()
            products = data.get("products") or data.get("results") or []
            # Collection endpoints need filtering
            needs_filter = i >= 2
            parsed = parse_shopify_products(products, store["url"], query if needs_filter else None)
            if parsed:
                await context.close()
                return parsed, None
        except Exception:
            continue

    await context.close()
    return [], "No results found"

# ── TCGPlayer Pro search via Playwright ───────────────────────────────────────

async def search_tcgpro_pw(store, query, browser):
    """
    Use Playwright to search a TCGPlayer Pro storefront.
    Intercepts the internal API call the page makes to get product data.
    """
    search_url = f"{store['url']}/search/products?productLineName=Magic%3A+The+Gathering&q={req.utils.quote(query)}"
    
    intercepted = []

    context = await browser.new_context(user_agent=UA)
    page = await context.new_page()

    # Intercept API responses from the TCGPlayer Pro backend
    async def on_response(response):
        url = response.url
        # TCGPlayer Pro calls its own API for search results
        if ("api" in url or "search" in url or "product" in url.lower()) and response.status == 200:
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    data = await response.json()
                    # Look for arrays of products in the response
                    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                        intercepted.append(data)
                    elif isinstance(data, dict):
                        for key in ["results", "products", "items", "data", "cards"]:
                            val = data.get(key)
                            if isinstance(val, list) and len(val) > 0:
                                intercepted.append(val)
                                break
                except Exception:
                    pass

    page.on("response", on_response)

    try:
        await page.goto(search_url, wait_until="networkidle", timeout=25000)
        # Give extra time for API calls to complete
        await page.wait_for_timeout(3000)
    except Exception:
        pass

    results = []

    # Process intercepted API data
    if intercepted:
        for item_list in intercepted:
            for item in item_list[:12]:
                if not isinstance(item, dict):
                    continue
                # TCGPlayer Pro product schema has various field names
                name = (item.get("name") or item.get("productName") or
                        item.get("title") or item.get("cleanName") or "")
                if not name:
                    continue

                # Price — look in common locations
                price = None
                for price_key in ["marketPrice", "lowPrice", "price", "lowestPrice", "minPrice"]:
                    v = item.get(price_key)
                    if v is not None:
                        try:
                            price = float(str(v).replace("$","").replace(",",""))
                            break
                        except Exception:
                            pass

                # Stock
                qty = item.get("quantity") or item.get("qty") or item.get("stock") or item.get("inventory") or 1
                available = int(qty) > 0 if str(qty).isdigit() else True

                if not available:
                    continue  # skip out of stock

                # URL
                slug = item.get("slug") or item.get("handle") or item.get("urlKey") or ""
                item_url = f"{store['url']}/product/{slug}" if slug else search_url

                set_name = (item.get("setName") or item.get("expansion") or
                            item.get("groupName") or extract_set(name))

                results.append({
                    "name":      clean_name(name),
                    "set":       set_name,
                    "price":     price,
                    "available": True,
                    "url":       item_url,
                })
    
    # Fallback: DOM scrape if API interception got nothing
    if not results:
        try:
            items = await page.evaluate("""
                () => {
                    const out = [];
                    // Try every plausible product card selector
                    const sel = [
                        '[class*="product-card"]', '[class*="ProductCard"]',
                        '[class*="search-result"]', '[class*="SearchResult"]',
                        'li[class*="product"]', 'article[class*="product"]',
                        '.product', '[data-testid*="product"]',
                    ];
                    let cards = [];
                    for (const s of sel) {
                        cards = [...document.querySelectorAll(s)];
                        if (cards.length) break;
                    }
                    // Also try any element containing a price
                    if (!cards.length) {
                        cards = [...document.querySelectorAll('*')].filter(el => {
                            const t = el.textContent;
                            return /\\$[0-9]+\\.[0-9]{2}/.test(t) && el.querySelectorAll('a').length > 0
                                   && el.children.length > 1 && el.children.length < 20;
                        }).slice(0, 10);
                    }
                    for (const card of cards.slice(0, 12)) {
                        const text = card.innerText || '';
                        const link = card.querySelector('a[href]');
                        const priceMatch = text.match(/\\$([0-9]+\\.[0-9]{2})/);
                        const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                        const outOfStock = /out.of.stock|sold.out|unavailable/i.test(text);
                        if (outOfStock) continue;
                        out.push({
                            text: lines.slice(0, 4).join(' | '),
                            price: priceMatch ? priceMatch[1] : null,
                            href: link ? link.href : null,
                        });
                    }
                    return out;
                }
            """)
            for item in items:
                name = item.get("text", "").split("|")[0].strip()
                if not name or len(name) < 2:
                    continue
                price_str = item.get("price")
                results.append({
                    "name":      name,
                    "set":       "",
                    "price":     float(price_str) if price_str else None,
                    "available": True,
                    "url":       item.get("href") or search_url,
                })
        except Exception:
            pass

    await context.close()

    if not results:
        return [], "No results found"
    return results, None

# ── Run all stores via single Playwright browser ──────────────────────────────

async def search_all_stores(query):
    async with async_playwright() as p:
        browser = await make_browser(p)
        try:
            # Run all stores concurrently
            shopify_tasks = [search_shopify_pw(s, query, browser) for s in SHOPIFY_STORES]
            tcg_tasks     = [search_tcgpro_pw(s, query, browser) for s in TCG_STORES]
            all_results   = await asyncio.gather(*shopify_tasks, *tcg_tasks)
        finally:
            await browser.close()

    output = []
    stores = SHOPIFY_STORES + TCG_STORES
    for store, (results, error) in zip(stores, all_results):
        entry = {
            "id":      store["id"],
            "name":    store["name"],
            "url":     store["url"],
            "results": results,
            "error":   error if not results else None,
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

    # Try cloudscraper first (fast)
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

    # Playwright fallback
    if not decks:
        try:
            async with async_playwright() as p:
                browser = await make_browser(p)
                context = await browser.new_context(user_agent=UA, extra_http_headers={
                    "Accept": "application/json",
                    "Referer": "https://www.moxfield.com/",
                })
                page = await context.new_page()
                await page.goto(f"https://www.moxfield.com/users/{username}",
                                wait_until="networkidle", timeout=20000)
                result = await page.evaluate(f"""
                    async () => {{
                        const r = await fetch("{api_url}", {{
                            headers: {{"Accept":"application/json","Referer":"https://www.moxfield.com/"}}
                        }});
                        return r.ok ? r.json() : null;
                    }}
                """)
                await browser.close()
                if result:
                    decks = result.get("data", [])
        except Exception as e:
            return {"username": username, "found_in": [], "total_decks": 0,
                    "error": f"Could not reach Moxfield: {e}",
                    "profile_url": f"https://www.moxfield.com/users/{username}",
                    "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}"}

    if not decks:
        return {"username": username, "found_in": [], "unknown": [], "total_decks": 0,
                "error": f"No public decks found for '{username}'.",
                "profile_url": f"https://www.moxfield.com/users/{username}",
                "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}"}

    found_in, unknown = [], []
    for deck in decks:
        deck_url  = f"https://www.moxfield.com/decks/{deck.get('publicId', deck.get('id',''))}"
        deck_info = {"name": deck.get("name","Unnamed"), "format": deck.get("format",""),
                     "url": deck_url}
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

    return {"username": username, "found_in": found_in, "unknown": unknown,
            "total_decks": len(decks),
            "profile_url": f"https://www.moxfield.com/users/{username}",
            "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}"}

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        stores = loop.run_until_complete(search_all_stores(query))
    finally:
        loop.close()
    return jsonify({"query": query, "stores": stores})

@app.route("/api/moxfield")
def api_moxfield():
    username = request.args.get("username", "").strip()
    card     = request.args.get("card", "").strip()
    if not username or not card:
        return jsonify({"error": "username and card required"}), 400
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(search_moxfield(username, card))
    finally:
        loop.close()
    return jsonify(result)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✦ NWA MTG Finder — http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
