"""
NWA MTG Local Store Finder — Backend
Single-store search endpoint: /api/search?q=<card>&store=<id>
"""

import os, re, asyncio, traceback, json
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests as req
import cloudscraper
from playwright.async_api import async_playwright

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

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_name(t): return re.sub(r"\s*\[.*?\]\s*","",t).strip()
def extract_set(t):
    m=re.search(r"\[(.+?)\]",t); return m.group(1) if m else ""

def parse_shopify(products, base_url, query):
    ql = query.lower()
    out = []
    for p in products:
        if ql not in (p.get("title") or "").lower(): continue
        variants = [v for v in (p.get("variants") or [{}]) if v.get("available")]
        if not variants: continue
        price = variants[0].get("price")
        out.append({
            "name":      clean_name(p.get("title") or ""),
            "set":       extract_set(p.get("title") or ""),
            "price":     float(price) if price else None,
            "available": True,
            "url":       f"{base_url}/products/{p.get('handle','')}",
        })
        if len(out) >= 15: break
    return out

# ── Shopify ───────────────────────────────────────────────────────────────────

def search_shopify(store, query):
    sc = cloudscraper.create_scraper()
    q  = req.utils.quote(query)

    def get(url):
        try:
            r = sc.get(url, headers=HEADERS, timeout=12)
            if not r.ok: return None
            if "json" not in r.headers.get("content-type","").lower(): return None
            return r.json()
        except Exception: return None

    # Try search endpoint first (1 request, server-filtered)
    for path in [f"/search?q={q}&type=product&view=json", f"/search?q={q}&view=json"]:
        d = get(store["url"] + path)
        if d:
            products = d.get("products") or d.get("results") or []
            parsed = parse_shopify(products, store["url"], query)
            if parsed: return parsed, None

    # Paginate collection, bail as soon as we find a match (cap 4 pages)
    for collection in [store["col"], "all"]:
        for pg in range(1, 5):
            d = get(f"{store['url']}/collections/{collection}/products.json?limit=250&page={pg}")
            if not d: break
            products = d.get("products", [])
            if not products: break
            parsed = parse_shopify(products, store["url"], query)
            if parsed: return parsed, None
            if len(products) < 250: return [], None
        break

    return [], None

# ── TCGPlayer Pro ─────────────────────────────────────────────────────────────

async def search_tcgpro(store, query):
    search_url = (f"{store['url']}/search/products"
                  f"?productLineName=Magic%3A+The+Gathering&q={req.utils.quote(query)}")
    domain     = store["url"].replace("https://","").split("/")[0]
    intercepted = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox",
                  "--disable-dev-shm-usage","--disable-gpu","--single-process"])
        context = await browser.new_context(user_agent=UA)
        page    = await context.new_page()

        async def on_resp(response):
            if domain not in response.url: return
            if response.status != 200: return
            if "json" not in response.headers.get("content-type",""): return
            if not any(k in response.url.lower() for k in ["search","product","catalog"]): return
            try:
                data = await response.json()
                cands = data if isinstance(data,list) else next(
                    (data[k] for k in ["results","products","items","data","cards"]
                     if isinstance(data.get(k),list) and data[k]), [])
                if cands and isinstance(cands[0],dict) and any(
                        k in cands[0] for k in ["name","productName","cleanName","title"]):
                    intercepted.append(cands)
            except Exception: pass

        page.on("response", on_resp)
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=25000)
            await page.wait_for_timeout(2000)
        except Exception: pass
        await browser.close()

    results = []
    for item_list in intercepted:
        for item in item_list[:15]:
            if not isinstance(item,dict): continue
            name = (item.get("name") or item.get("productName") or
                    item.get("cleanName") or item.get("title") or "")
            if not name: continue
            price = None
            for pk in ["marketPrice","lowPrice","price","lowestPrice","minPrice","retailPrice","salePrice"]:
                v = item.get(pk)
                if v is not None:
                    try:
                        price = float(str(v).replace("$","").replace(",",""))
                        if price > 0: break
                    except Exception: pass
            if price is None:
                for pk2 in ["pricing","prices","priceData"]:
                    nested = item.get(pk2)
                    if isinstance(nested,dict):
                        for pk3 in ["market","low","mid","direct"]:
                            v = nested.get(pk3)
                            if v:
                                try:
                                    price = float(str(v).replace("$","").replace(",",""))
                                    if price > 0: break
                                except Exception: pass
                    if price: break
            qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
            try:
                if int(str(qty).split(".")[0]) <= 0: continue
            except Exception: pass
            slug = item.get("slug") or item.get("handle") or item.get("urlKey") or ""
            item_url = (f"{store['url']}/product/{slug.lstrip('/')}"
                        if slug and not slug.startswith("http") else slug or search_url)
            results.append({"name":clean_name(name),"set":item.get("setName") or
                item.get("groupName") or extract_set(name),
                "price":price,"available":True,"url":item_url})
        if results: break

    return (results, None) if results else ([], "No results found")

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
            results, err = loop.run_until_complete(search_tcgpro(store, query))
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
    try:
        results, err = search_shopify(STORES["finalboss"], "plains")
        return jsonify({"finalboss":{"matches":len(results),"first":results[0]["name"] if results else "none","error":err}})
    except Exception as e:
        return jsonify({"error":str(e)})

@app.route("/health")
def health(): return jsonify({"status":"ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)
