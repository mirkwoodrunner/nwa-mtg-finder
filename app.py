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
    """All query words must appear as whole words in title (not as substrings of longer words)."""
    tl = title.lower()
    ql = query.lower()
    if ql in tl:
        return True
    words = ql.split()
    return all(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in words) if words else False

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

def parse_next_link(link_header):
    """Extract the rel=next URL from a Shopify Link header, or return None."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None

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

    # 1. Try Shopify/BinderPOS search endpoint with pagination.
    #    BinderPOS returns ≤20 results per page; paginate up to 5 pages.
    #    Also try the Shopify predictive search API which works on all storefronts.
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

    # Shopify predictive search — always available regardless of theme/template.
    predictive_url = (f"{store['url']}/search/suggest.json"
                      f"?q={q}&resources[type]=product&resources[limit]=20")
    d = get_json_with_retry(sc, predictive_url)
    if d:
        resources = d.get("resources", {}).get("results", {})
        products = resources.get("products", [])
        add_results(parse_shopify(products, store["url"], query))

    # 2. Paginate collection — cursor-aware for Gear Gaming, numeric fallback for Final Boss.
    for collection in [store["col"], "all"]:
        collection_had_pages = False
        next_url = f"{store['url']}/collections/{collection}/products.json?limit=250"
        page_num = 1
        cursor_page = 0

        while next_url:
            try:
                r = sc.get(next_url, headers=HEADERS, timeout=12)
                if not r.ok:
                    break
                ct = r.headers.get("content-type", "").lower()
                if "json" not in ct:
                    break
                d = r.json()
            except Exception:
                break

            products = d.get("products", [])
            if not products:
                break

            collection_had_pages = True
            add_results(parse_shopify(products, store["url"], query))

            link_header = r.headers.get("Link") or r.headers.get("link") or ""
            next_link = parse_next_link(link_header)

            if next_link:
                cursor_page += 1
                if cursor_page >= 40:
                    break
                next_url = next_link
            elif len(products) == 250:
                page_num += 1
                next_url = f"{store['url']}/collections/{collection}/products.json?limit=250&page={page_num}"
            else:
                next_url = None

            if page_num > 20 and not next_link:
                break

        if collection_had_pages:
            break

    return (all_results, None) if all_results else ([], None)

# ── TCGPlayer Pro ─────────────────────────────────────────────────────────────

def _parse_tcgpro_products(data, store, query, fallback_url):
    """Walk any dict/list tree to find product-like item lists and filter by query."""
    def _find_product_lists(obj, depth=0):
        if depth > 8:
            return []
        if isinstance(obj, list):
            return [obj] if obj and isinstance(obj[0], dict) else []
        if isinstance(obj, dict):
            found = []
            for v in obj.values():
                found.extend(_find_product_lists(v, depth + 1))
            return found
        return []

    name_keys = ["name","productName","cleanName","title","productTitle","cardName"]
    results = []
    seen_ids = set()

    for candidate_list in _find_product_lists(data):
        has_name = any(any(k in item for k in name_keys) for item in candidate_list[:5])
        if not has_name:
            continue
        for item in candidate_list:
            if not isinstance(item, dict):
                continue
            name = next((item.get(k) for k in name_keys if item.get(k)), "")
            if not name or not name_matches(name, query):
                continue
            dedup_key = item.get("productId") or item.get("id") or name
            if dedup_key in seen_ids:
                continue
            seen_ids.add(dedup_key)
            qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
            try:
                if int(str(qty).split(".")[0]) <= 0:
                    continue
            except Exception:
                pass
            results.append({
                "name":      clean_name(name),
                "set":       item.get("setName") or item.get("groupName") or extract_set(name),
                "price":     _extract_tcg_price(item),
                "available": True,
                "url":       _build_tcgpro_url(item, store, fallback_url),
            })

    return results


def _search_tcgpro_http(store, query):
    """POST directly to TCGPlayer Pro's catalog search API."""
    base_url = store["url"]
    api_url  = f"{base_url}/api/catalog/search"
    sid      = store["id"]
    referer  = (f"{base_url}/search/products"
                f"?productLineName=Magic%3A+The+Gathering&q={req.utils.quote(query)}")

    api_headers = {
        "User-Agent":      UA,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type":    "application/json",
        "Origin":          base_url,
        "Referer":         referer,
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-origin",
    }
    payload = {
        "query":   query,
        "context": {"productLineName": "Magic: The Gathering"},
        "filters": {},
        "from":    0,
        "size":    48,
        "sort":    [{"field": "in-stock-price-sort", "order": "asc"}],
    }

    try:
        session = req.Session()
        # Seed the ASP.NET session cookie with a quick GET before the API call.
        session.get(base_url, headers={"User-Agent": UA}, timeout=15)

        r = session.post(api_url, json=payload, headers=api_headers, timeout=20)
        print(f"[{sid}] API POST {r.status_code} ct={r.headers.get('content-type','')[:50]}", flush=True)

        if r.status_code != 200:
            print(f"[{sid}] API non-200: {r.text[:200]}", flush=True)
            return None

        data = r.json()
        print(f"[{sid}] API response keys: {list(data.keys())}", flush=True)

        results = _parse_tcgpro_products(data, store, query, referer)
        print(f"[{sid}] API: {len(results)} result(s)", flush=True)
        return results

    except Exception as ex:
        print(f"[{sid}] API exception: {ex}", flush=True)
        return None


async def search_tcgpro(store, query):
    search_url = (f"{store['url']}/search/products"
                  f"?productLineName=Magic%3A+The+Gathering&q={req.utils.quote(query)}")
    sid = store["id"]

    # Primary: direct HTTP fetch (fast, avoids Playwright bot detection)
    results = _search_tcgpro_http(store, query)
    if results is not None:
        return (results, None)

    # Fallback: Playwright (handles JS-rendered pages)
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

            try:
                await page.goto(search_url, wait_until="networkidle", timeout=25000)
            except Exception:
                pass

            results = await _scrape_tcgpro_dom(page, store, query, search_url)
            print(f"[{sid}] Playwright DOM: {len(results)} result(s)", flush=True)
            await browser.close()

    except Exception as e:
        traceback.print_exc()
        return ([], str(e))

    return (results, None) if results else ([], None)


def _extract_tcg_price(item):
    """Extract the best available price from a TCGPlayer Pro item dict."""
    for pk in ["lowestListingPrice","marketPrice","lowPrice","price","lowestPrice",
               "minPrice","retailPrice","salePrice","directLowPrice","normalPrice"]:
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

        # Cursor pagination probe: follow up to 5 pages of the named collection
        # and report whether Link headers are being returned.
        cursor_probe = {"pages": 0, "total_products": 0, "has_link_header": False, "query_matches": 0}
        next_url = f"{store['url']}/collections/{store['col']}/products.json?limit=250"
        for _ in range(5):
            try:
                r = sc.get(next_url, headers=HEADERS, timeout=12)
                if not r.ok:
                    cursor_probe["stopped"] = f"HTTP {r.status_code} on page {cursor_probe['pages']+1}"
                    cursor_probe["body_preview"] = r.text[:200].strip()
                    break
                ct = r.headers.get("content-type", "")
                if "json" not in ct:
                    cursor_probe["stopped"] = f"non-JSON ({ct[:60]}) on page {cursor_probe['pages']+1}"
                    break
                d = r.json()
                products = d.get("products", [])
                cursor_probe["pages"] += 1
                cursor_probe["total_products"] += len(products)
                matches = [p for p in products if name_matches(p.get("title", ""), query)]
                cursor_probe["query_matches"] += len(matches)
                if cursor_probe["pages"] == 1:
                    cursor_probe["first_title"] = products[0].get("title", "")[:80] if products else ""

                link_header = r.headers.get("Link") or r.headers.get("link") or ""
                next_link = parse_next_link(link_header)
                if next_link:
                    cursor_probe["has_link_header"] = True
                    next_url = next_link
                elif len(products) == 250:
                    cursor_probe["numeric_fallback_triggered"] = True
                    # Don't actually follow numeric fallback — just note it and stop
                    break
                else:
                    break
            except Exception as e:
                cursor_probe["error"] = str(e)[:120]
                break
        probe["cursor_pagination"] = cursor_probe

        out[sid] = probe

    return jsonify(out)

@app.route("/debug-panel")
def debug_panel(): return send_from_directory("static", "debug.html")

@app.route("/health")
def health(): return jsonify({"status":"ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)
