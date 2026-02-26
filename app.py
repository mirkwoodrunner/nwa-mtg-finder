"""
NWA MTG Local Store Finder — Backend
Flask + Playwright for TCGPlayer Pro stores, Moxfield
requests for BinderPOS/Shopify stores
"""

import os, re, json, asyncio
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
    {"id": "xxplo",  "name": "Games Xxplosion",  "url": "https://gamesexxplosion.tcgplayerpro.com"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_name(title):
    return re.sub(r"\s*\[.*?\]\s*", "", title).strip()

def extract_set(title):
    m = re.search(r"\[(.+?)\]", title)
    return m.group(1) if m else ""

def parse_products(products, base_url, filter_query=None):
    if filter_query:
        ql = filter_query.lower()
        products = [p for p in products if ql in (p.get("title") or "").lower()]
    if not products:
        return []
    results = []
    for p in products[:10]:
        variants = p.get("variants") or [{}]
        price, available = None, False
        for v in variants:
            if v.get("available"):
                price = v.get("price")
                available = True
                break
        if not price and variants:
            price = variants[0].get("price")
        available = available or any(v.get("available") for v in variants)
        url = p.get("url")
        url = f"{base_url}{url}" if url else f"{base_url}/products/{p.get('handle','')}"
        results.append({
            "name":      clean_name(p.get("title") or ""),
            "set":       extract_set(p.get("title") or ""),
            "price":     float(price) if price else None,
            "available": bool(available),
            "url":       url,
        })
    return results

# ── Shopify / BinderPOS search ────────────────────────────────────────────────

def search_shopify(store, query):
    q = query
    endpoints = [
        (f"{store['url']}/search",       {"q": q, "type": "product", "view": "json"},    False),
        (f"{store['url']}/search.json",  {"q": q, "type": "product", "limit": 12},       False),
        (f"{store['url']}/collections/{store['col']}/products.json", {"limit": 250},      True),
        (f"{store['url']}/collections/all/products.json",            {"limit": 250},      True),
    ]
    for url, params, needs_filter in endpoints:
        try:
            r = req.get(url, params=params, headers=HEADERS, timeout=10)
            if not r.ok:
                continue
            data = r.json()
            products = data.get("products") or data.get("results") or []
            parsed = parse_products(products, store["url"], q if needs_filter else None)
            if parsed:
                return parsed, None
        except Exception:
            continue
    return [], "Could not reach store inventory"

# ── Playwright / TCGPlayer Pro search ────────────────────────────────────────

async def search_tcgpro(store, query):
    search_url = (
        f"{store['url']}/search/products"
        f"?productLineName=Magic%3A+The+Gathering"
        f"&q={req.utils.quote(query)}"
    )
    results = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page()
            await page.set_extra_http_headers({"User-Agent": HEADERS["User-Agent"]})

            await page.goto(search_url, wait_until="networkidle", timeout=25000)

            # Wait for product cards to appear
            try:
                await page.wait_for_selector("[data-testid='product-card'], .product-card, .search-result-item, .product-list-item", timeout=10000)
            except Exception:
                pass  # May have a different structure — try to scrape anyway

            # Extract product data from the page
            items = await page.evaluate("""
                () => {
                    const results = [];
                    // TCGPlayer Pro uses various selectors depending on version
                    const selectors = [
                        '[data-testid="product-card"]',
                        '.product-card',
                        '.search-result',
                        'article[class*="product"]',
                        '[class*="ProductCard"]',
                        '[class*="product-card"]',
                    ];
                    let cards = [];
                    for (const sel of selectors) {
                        cards = Array.from(document.querySelectorAll(sel));
                        if (cards.length > 0) break;
                    }
                    for (const card of cards.slice(0, 10)) {
                        const nameEl = card.querySelector('[class*="name"], [class*="title"], h3, h4, .product-name');
                        const priceEl = card.querySelector('[class*="price"], .price, [data-testid*="price"]');
                        const linkEl  = card.querySelector('a[href]');
                        const stockEl = card.querySelector('[class*="stock"], [class*="inventory"], [class*="qty"]');
                        const setEl   = card.querySelector('[class*="set"], [class*="expansion"]');
                        const name  = nameEl  ? nameEl.textContent.trim()  : null;
                        const price = priceEl ? priceEl.textContent.trim() : null;
                        const href  = linkEl  ? linkEl.href                : null;
                        const stock = stockEl ? stockEl.textContent.trim() : null;
                        const set   = setEl   ? setEl.textContent.trim()   : null;
                        if (name) results.push({ name, price, href, stock, set });
                    }
                    return results;
                }
            """)

            await browser.close()

            for item in items:
                raw_price = item.get("price") or ""
                price_match = re.search(r"[\d]+\.[\d]{2}", raw_price.replace(",", ""))
                price = float(price_match.group()) if price_match else None
                stock_text = (item.get("stock") or "").lower()
                available = "out" not in stock_text and "0" not in stock_text

                results.append({
                    "name":      item.get("name") or "",
                    "set":       item.get("set") or "",
                    "price":     price,
                    "available": available,
                    "url":       item.get("href") or search_url,
                })

    except Exception as e:
        return [], f"Playwright error: {str(e)}"

    return results, None

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400

    output = []

    # Shopify stores — synchronous requests
    for store in SHOPIFY_STORES:
        results, error = search_shopify(store, query)
        output.append({
            "id": store["id"], "name": store["name"], "url": store["url"],
            "type": "live", "results": results, "error": error,
        })

    # TCGPlayer Pro stores — async Playwright
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_tcg():
        tasks = [search_tcgpro(store, query) for store in TCG_STORES]
        return await asyncio.gather(*tasks)

    try:
        tcg_results = loop.run_until_complete(run_tcg())
    finally:
        loop.close()

    for store, (results, error) in zip(TCG_STORES, tcg_results):
        output.append({
            "id": store["id"], "name": store["name"], "url": store["url"],
            "type": "live", "results": results, "error": error,
            "search_url": (
                f"{store['url']}/search/products"
                f"?productLineName=Magic%3A+The+Gathering"
                f"&q={req.utils.quote(query)}"
            ),
        })

    return jsonify({"query": query, "stores": output})

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


async def search_moxfield(username, card_name):
    """
    Fetch user's public decks from Moxfield and check which contain card_name.
    Tries cloudscraper first (faster), falls back to Playwright if blocked.
    """
    api_url = f"https://api2.moxfield.com/v2/users/{username}/decks?pageNumber=1&pageSize=100&sortType=updated&sortDirection=Descending"
    card_lower = card_name.lower()

    decks = []

    # ── Attempt 1: cloudscraper (fast, handles Cloudflare JS challenge) ──
    try:
        scraper = cloudscraper.create_scraper()
        r = scraper.get(api_url, headers={
            "Accept": "application/json",
            "Referer": "https://www.moxfield.com/",
            "Origin": "https://www.moxfield.com",
        }, timeout=15)
        if r.ok:
            data = r.json()
            decks = data.get("data", [])
    except Exception:
        pass

    # ── Attempt 2: Playwright (full browser, most reliable) ──
    if not decks:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                    extra_http_headers={
                        "Accept": "application/json",
                        "Referer": "https://www.moxfield.com/",
                    }
                )
                page = await context.new_page()

                # Visit profile page first to get cookies/tokens
                await page.goto(f"https://www.moxfield.com/users/{username}", wait_until="networkidle", timeout=20000)

                # Now fetch the API from within the page context (has cookies)
                result = await page.evaluate(f"""
                    async () => {{
                        const r = await fetch("{api_url}", {{
                            headers: {{ "Accept": "application/json", "Referer": "https://www.moxfield.com/" }}
                        }});
                        return r.ok ? r.json() : null;
                    }}
                """)
                await browser.close()

                if result:
                    decks = result.get("data", [])
        except Exception as e:
            return {
                "username": username, "found_in": [], "total_decks": 0,
                "error": f"Could not reach Moxfield: {str(e)}",
                "profile_url": f"https://www.moxfield.com/users/{username}",
                "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
            }

    if not decks:
        return {
            "username": username, "found_in": [], "unknown": [], "total_decks": 0,
            "error": f"No public decks found for '{username}'. Check the username is correct and decks are set to public.",
            "profile_url": f"https://www.moxfield.com/users/{username}",
            "search_url": f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
        }

    found_in = []
    unknown  = []

    for deck in decks:
        deck_url = f"https://www.moxfield.com/decks/{deck.get('publicId', deck.get('id', ''))}"
        deck_info = {
            "name":    deck.get("name", "Unnamed Deck"),
            "format":  deck.get("format", ""),
            "url":     deck_url,
            "colors":  deck.get("colorIdentity", ""),
        }

        # Check all zones for the card
        found = False
        has_card_data = False
        for zone in ["mainboard", "commanders", "sideboard", "maybeboard", "companion"]:
            zone_cards = deck.get(zone, {})
            if isinstance(zone_cards, dict) and zone_cards:
                has_card_data = True
                for card_key in zone_cards:
                    if card_lower in card_key.lower():
                        found = True
                        break
            if found:
                break

        if has_card_data:
            if found:
                found_in.append(deck_info)
            # If has_card_data but not found, it's definitively NOT in deck — skip
        else:
            # Summary didn't include card lists — flag as unknown
            unknown.append(deck_info)

    return {
        "username":    username,
        "found_in":    found_in,
        "unknown":     unknown,
        "total_decks": len(decks),
        "profile_url": f"https://www.moxfield.com/users/{username}",
        "search_url":  f"https://www.moxfield.com/search#q={req.utils.quote(card_name)}",
    }
@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✦ NWA MTG Finder running on http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
