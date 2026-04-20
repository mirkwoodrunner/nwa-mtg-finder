"""
NWA MTG Local Store Finder — Backend
Single-store endpoint: /api/search?q=<card>&store=<id>
"""
import os, re, asyncio, traceback, json, time
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests as req
import cloudscraper
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

app = Flask(__name__, static_folder="static")
CORS(app)

STORES = {
    "finalboss": {"id":"finalboss","name":"Final Boss Games",          "type":"shopify","url":"https://finalbossgames.com",              "col":"singles"},
    "gearbv":    {"id":"gearbv",   "name":"Gear Gaming — Bentonville", "type":"shopify","url":"https://bentonville.geargamingstore.com", "col":"mtg-singles-all-products"},
    "gearfv":    {"id":"gearfv",   "name":"Gear Gaming — Fayetteville","type":"shopify","url":"https://fayetteville.geargamingstore.com","col":"mtg-singles-all-products"},
    "chaos":     {"id":"chaos",    "name":"Chaos Games",               "type":"tcg",    "url":"https://chaosgamesnwa.tcgplayerpro.com"},
    "xxplo":     {"id":"xxplo",    "name":"Games Explosion",           "type":"tcg",    "url":"https://gamesexxplosion.tcgplayerpro.com"},
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent":UA,"Accept":"application/json,text/html,*/*","Accept-Language":"en-US,en;q=0.9"}

def clean_name(t): return re.sub(r"\s*\[.*?\]\s*","",t).strip()
def extract_set(t):
    m=re.search(r"\[(.+?)\]",t); return m.group(1) if m else ""

def name_matches(title, query):
    """Fuzzy match: all query words must appear in title (handles reordering/extra words)."""
    tl = title.lower()
    ql = query.lower()
    if ql in tl:
        return True
    words = [w for w in ql.split() if len(w) > 2]
    return all(w in tl for w in words) if words else False

def parse_shopify(products, base_url, query):
    """Parse a batch of Shopify products, returning ALL variants that match the query."""
    out = []
    for p in products:
        title = (p.get("title") or "")
        if not name_matches(title, query):
            continue
        variants = p.get("variants") or [{}]
        for v in variants:
            if not v.get("available"):
                continue
            price = v.get("price")
            variant_title = v.get("title", "")
            display_name = title
            if variant_title and variant_title.lower() not in ("default title", ""):
                display_name = f"{title} — {variant_title}"
            out.append({
                "name":      clean_name(display_name),
                "set":       extract_set(title),
                "price":     float(price) if price else None,
                "available": True,
                "url":       f"{base_url}/products/{p.get('handle','')}",
            })
    return out

# ── Shopify ───────────────────────────────────────────────────────────────────

def get_json_with_retry(sc, url, retries=2, timeout=12):
    """Fetch JSON from URL with simple retry logic."""
    for attempt in range(retries + 1):
        try:
            r = sc.get(url, headers=HEADERS, timeout=timeout)
            if not r.ok:
                if attempt < retries:
                    time.sleep(1)
                    continue
                return None
            ct = r.headers.get("content-type", "").lower()
            if "json" not in ct:
                return None
            return r.json()
        except Exception:
            if attempt < retries:
                time.sleep(1)
            continue
    return None

def _find_mtg_collection(sc, store):
    """Enumerate /collections.json to discover the real MTG singles collection slug."""
    try:
        r = sc.get(f"{store['url']}/collections.json?limit=100", headers=HEADERS, timeout=12)
        if not r.ok or "json" not in r.headers.get("content-type", "").lower():
            return None
        cols = r.json().get("collections", [])
        # Prefer most specific MTG singles slugs first
        for kw in ["mtg-singles", "magic-singles", "singles", "mtg", "magic"]:
            for c in cols:
                if kw in c.get("handle", "").lower():
                    return c["handle"]
    except Exception:
        pass
    return None

def search_shopify(store, query):
    sc = cloudscraper.create_scraper()
    q  = req.utils.quote(query)

    all_results = []
    seen_keys = set()

    def add_results(parsed):
        for r in parsed:
            key = (r["url"], r.get("name", ""))
            if key not in seen_keys:
                seen_keys.add(key)
                all_results.append(r)

    # 1. Try Shopify/BinderPOS search endpoint with pagination
    #    BinderPOS returns ≤20 results per page; paginate up to 5 pages.
    search_endpoint_worked = False
    for path_template in [
        f"/search?q={q}&type=product&view=json",
        f"/search?q={q}&view=json",
        f"/search.json?q={q}&type=product",
    ]:
        sep = "&" if "?" in path_template else "?"
        for pg in range(1, 6):
            page_path = f"{path_template}{sep}page={pg}" if pg > 1 else path_template
            d = get_json_with_retry(sc, store["url"] + page_path)
            if d is None:
                break
            products = d.get("products") or d.get("results") or []
            if not isinstance(products, list):
                break
            add_results(parse_shopify(products, store["url"], query))
            search_endpoint_worked = True
            if len(products) < 20:
                break  # last page
        if search_endpoint_worked:
            break  # first working search endpoint wins

    # 2. Paginate collection — always runs on top of search to catch anything missed.
    #    If the configured slug returns nothing, discover the real slug via /collections.json,
    #    then fall back to the "all" collection.
    configured_had_pages = False
    for pg in range(1, 9):
        d = get_json_with_retry(sc, f"{store['url']}/collections/{store['col']}/products.json?limit=250&page={pg}")
        if d is None:
            break
        products = d.get("products", [])
        if not products:
            break
        configured_had_pages = True
        add_results(parse_shopify(products, store["url"], query))
        if len(products) < 250:
            break

    if not configured_had_pages:
        discovered = _find_mtg_collection(sc, store)
        fallback_col = discovered if (discovered and discovered != store["col"]) else "all"
        for pg in range(1, 9):
            d = get_json_with_retry(sc, f"{store['url']}/collections/{fallback_col}/products.json?limit=250&page={pg}")
            if d is None:
                break
            products = d.get("products", [])
            if not products:
                break
            add_results(parse_shopify(products, store["url"], query))
            if len(products) < 250:
                break

    return (all_results, None) if all_results else ([], None)

# ── TCGPlayer Pro ─────────────────────────────────────────────────────────────

async def search_tcgpro(store, query):
    search_url = (f"{store['url']}/search/products"
                  f"?productLineName=Magic%3A+The+Gathering&q={req.utils.quote(query)}")
    intercepted = []
    dom_results = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-dev-shm-usage","--disable-gpu","--single-process"])
            context = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await context.new_page()

            await stealth_async(page)

            async def on_resp(response):
                try:
                    url = response.url
                    # Capture responses from any tcgplayer subdomain (store domain
                    # OR shared API hosts like catalog.tcgplayer.com)
                    if "tcgplayer" not in url.lower():
                        return
                    if response.status != 200:
                        return
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    skip_keywords = ["analytics", "telemetry", "tracking", "gtm", "segment",
                                     "hotjar", "sentry", "favicon", "font", "css"]
                    if any(k in url.lower() for k in skip_keywords):
                        return
                    try:
                        data = await response.json()
                    except Exception:
                        return
                    cands = []
                    if isinstance(data, list):
                        cands = data
                    elif isinstance(data, dict):
                        for key in ["results","products","items","data","cards","catalog",
                                    "inventory","listings","skus"]:
                            val = data.get(key)
                            if isinstance(val, list) and val:
                                cands = val
                                break
                        if not cands and isinstance(data.get("data"), dict):
                            for key in ["results","products","items","cards"]:
                                val = data["data"].get(key)
                                if isinstance(val, list) and val:
                                    cands = val
                                    break
                    if not cands:
                        return
                    if not isinstance(cands[0], dict):
                        return
                    if any(k in cands[0] for k in ["name","productName","cleanName","title",
                                                     "productTitle","cardName"]):
                        intercepted.append((url, cands))
                except Exception:
                    pass

            page.on("response", on_resp)

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                for _ in range(12):
                    await page.wait_for_timeout(1000)
                    if intercepted:
                        break
            except Exception:
                pass

            if not intercepted:
                try:
                    await page.wait_for_timeout(3000)
                    dom_results = await _scrape_tcgpro_dom(page, store, query, search_url)
                except Exception:
                    pass

            await browser.close()

    except Exception:
        return ([], None)

    results = []
    seen_names = set()
    for (intercept_url, item_list) in intercepted:
        for item in item_list:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or item.get("productName") or
                    item.get("cleanName") or item.get("title") or
                    item.get("productTitle") or item.get("cardName") or "")
            if not name:
                continue
            if not name_matches(name, query):
                continue
            if name in seen_names:
                continue
            seen_names.add(name)

            price = _extract_tcg_price(item)

            qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
            try:
                if int(str(qty).split(".")[0]) <= 0:
                    continue
            except Exception:
                pass

            results.append({
                "name":      clean_name(name),
                "set":       item.get("setName") or item.get("groupName") or extract_set(name),
                "price":     price,
                "available": True,
                "url":       _build_tcgpro_url(item, store, search_url),
            })

    if not results and dom_results:
        results = dom_results

    return (results, None) if results else ([], None)

def _extract_tcg_price(item):
    """Extract the best available price from a TCGPlayer Pro item dict."""
    for pk in ["marketPrice","lowPrice","price","lowestPrice","minPrice",
               "retailPrice","salePrice","directLowPrice","normalPrice"]:
        v = item.get(pk)
        if v is not None:
            try:
                price = float(str(v).replace("$","").replace(",",""))
                if price > 0:
                    return price
            except Exception:
                pass
    for pk2 in ["pricing","prices","priceData","marketPrices"]:
        nested = item.get(pk2)
        if isinstance(nested, dict):
            for pk3 in ["market","low","mid","direct","normal","retail"]:
                v = nested.get(pk3)
                if v:
                    try:
                        price = float(str(v).replace("$","").replace(",",""))
                        if price > 0:
                            return price
                    except Exception:
                        pass
    return None

def _build_tcgpro_url(item, store, fallback_url):
    """Build the best product URL for a TCGPlayer Pro item."""
    for url_key in ["url","productUrl","link","href"]:
        v = item.get(url_key)
        if v and isinstance(v, str) and v.startswith("http"):
            return v

    slug = item.get("slug") or item.get("handle") or item.get("urlKey") or ""
    product_line = item.get("productLineName") or item.get("categoryName") or ""

    if slug:
        if slug.startswith("http"):
            return slug
        pl_slug = product_line.lower().replace(" ","_").replace(":","") if product_line else "magic_the_gathering"
        return f"{store['url']}/product/{pl_slug}/{slug.lstrip('/')}"

    pid = item.get("productId") or item.get("id")
    if pid:
        return f"{store['url']}/product/{pid}"

    return fallback_url

async def _scrape_tcgpro_dom(page, store, query, fallback_url):
    """Fallback: scrape rendered product cards from TCGPlayer Pro DOM."""
    results = []
    ql = query.lower()
    try:
        selectors = [
            "[data-testid='product-card']",
            ".product-card",
            ".search-result",
            "[class*='ProductCard']",
            "[class*='product-tile']",
            "article",
        ]
        for sel in selectors:
            cards = await page.query_selector_all(sel)
            if not cards:
                continue
            for card in cards[:20]:
                try:
                    text = await card.inner_text()
                    if ql not in text.lower():
                        continue
                    anchor = await card.query_selector("a")
                    href = await anchor.get_attribute("href") if anchor else ""
                    if href and not href.startswith("http"):
                        href = store["url"] + href
                    price_match = re.search(r'\$(\d+\.\d{2})', text)
                    price = float(price_match.group(1)) if price_match else None
                    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
                    name = lines[0] if lines else query
                    results.append({
                        "name":      clean_name(name),
                        "set":       extract_set(name),
                        "price":     price,
                        "available": True,
                        "url":       href or fallback_url,
                    })
                except Exception:
                    continue
            if results:
                break
    except Exception:
        pass
    return results

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return send_from_directory("static","index.html")

@app.route("/api/search")
def api_search():
    query    = request.args.get("q","").strip()
    store_id = request.args.get("store","").strip()
    if not query:    return jsonify({"error":"No query"}), 400
    if store_id not in STORES: return jsonify({"error":f"Unknown store '{store_id}'"}), 400

    store = STORES[store_id]
    try:
        if store["type"] == "shopify":
            results, err = search_shopify(store, query)
        else:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                results, err = loop.run_until_complete(search_tcgpro(store, query))
            finally:
                loop.close()

        return jsonify({
            "query":   query,
            "store":   store_id,
            "results": results or [],
            "error":   err if not results else None,
            "search_url": (
                f"{store['url']}/search/products?productLineName=Magic%3A+The+Gathering&q={req.utils.quote(query)}"
                if store["type"] == "tcg"
                else f"{store['url']}/search?q={req.utils.quote(query)}&type=product"
            ),
        })
    except Exception as e:
        return jsonify({"error":str(e),"trace":traceback.format_exc()}), 500

@app.route("/api/debug")
def api_debug():
    """Verbose per-URL probe of every Shopify endpoint so we can see exactly what's failing."""
    sc = cloudscraper.create_scraper()
    query = request.args.get("q", "lightning bolt")
    q = req.utils.quote(query)
    out = {}

    for sid in ["finalboss", "gearbv", "gearfv"]:
        store = STORES[sid]
        probe = {}

        urls_to_try = [
            ("search_json",   f"{store['url']}/search?q={q}&type=product&view=json"),
            ("search_plain",  f"{store['url']}/search?q={q}&view=json"),
            ("col_page1",     f"{store['url']}/collections/{store['col']}/products.json?limit=10&page=1"),
            ("all_page1",     f"{store['url']}/collections/all/products.json?limit=10&page=1"),
        ]
        for label, url in urls_to_try:
            try:
                r = sc.get(url, headers=HEADERS, timeout=12)
                ct = r.headers.get("content-type", "")
                entry = {"status": r.status_code, "ct": ct[:80], "url": url}
                if "json" in ct:
                    try:
                        d = r.json()
                        products = d.get("products") or d.get("results") or []
                        entry["products"] = len(products)
                        if products:
                            entry["first_title"] = products[0].get("title","?")[:80]
                            matches = [p for p in products if name_matches(p.get("title",""), query)]
                            entry["query_matches"] = len(matches)
                            if matches:
                                entry["match_sample"] = matches[0].get("title","")[:80]
                    except Exception as je:
                        entry["json_err"] = str(je)
                else:
                    entry["body_preview"] = r.text[:200].strip()
            except Exception as e:
                entry = {"error": str(e)[:120]}
            probe[label] = entry

        out[sid] = probe

    return jsonify(out)

@app.route("/health")
def health(): return jsonify({"status":"ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)
