"""
Standalone HTTP probe of all five NWA MTG stores.
No Flask app required — probes upstream stores directly.
Run: python investigate.py
"""
import re, sys, json
import cloudscraper
import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/html,*/*", "Accept-Language": "en-US,en;q=0.9"}

QUERY = "Lightning Bolt"
Q = requests.utils.quote(QUERY)

SHOPIFY_STORES = [
    {"id": "finalboss", "url": "https://finalbossgames.com",              "col": "singles"},
    {"id": "gearbv",    "url": "https://bentonville.geargamingstore.com", "col": "mtg-singles-all-products"},
    {"id": "gearfv",    "url": "https://fayetteville.geargamingstore.com","col": "mtg-singles-all-products"},
]

TCG_STORES = [
    {"id": "chaos", "url": "https://chaosgamesnwa.tcgplayerpro.com"},
    {"id": "xxplo", "url": "https://gamesexxplosion.tcgplayerpro.com"},
]


def name_matches(title, query):
    tl = title.lower()
    ql = query.lower()
    if ql in tl:
        return True
    words = ql.split()
    return all(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in words) if words else False


def probe_url(sc, url, method="GET", json_payload=None, extra_headers=None, session=None):
    """Probe a URL and return a summary dict."""
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    try:
        if method == "POST" and session:
            r = session.post(url, json=json_payload, headers=h, timeout=20)
        elif method == "POST":
            r = sc.post(url, json=json_payload, headers=h, timeout=20)
        else:
            if session:
                r = session.get(url, headers=h, timeout=20)
            else:
                r = sc.get(url, headers=h, timeout=20)

        ct = r.headers.get("content-type", "")
        result = {
            "status": r.status_code,
            "ct": ct[:80],
            "final_url": r.url,
            "cloudflare": ("cloudflare" in r.text.lower() or "challenge" in r.text.lower()),
        }

        link_header = r.headers.get("Link") or r.headers.get("link") or ""
        if link_header:
            result["link_header"] = link_header[:200]

        if "json" in ct.lower():
            try:
                d = r.json()
                result["json_keys"] = list(d.keys())[:10]
                # Extract product list depending on endpoint type
                if "resources" in d:
                    products = d.get("resources", {}).get("results", {}).get("products", [])
                else:
                    products = d.get("products") or d.get("results") or []

                if not isinstance(products, list):
                    products = []

                result["product_count"] = len(products)
                if products and isinstance(products[0], dict):
                    result["first_title"] = products[0].get("title", products[0].get("name", "?"))[:80]
                    matches = [p for p in products if name_matches(p.get("title", p.get("name", "")), QUERY)]
                    result["name_match_count"] = len(matches)
                else:
                    result["product_count"] = len(products)
                    result["name_match_count"] = 0
            except Exception as e:
                result["json_parse_error"] = str(e)[:100]
        else:
            body = r.text[:300]
            result["body_preview"] = body.strip()

        return result

    except Exception as e:
        return {"error": str(e)[:200]}


def probe_shopify(store):
    sc = cloudscraper.create_scraper()
    base = store["url"]
    col = store["col"]

    endpoints = [
        ("search?type+view=json",           f"{base}/search?q={Q}&type=product&view=json"),
        ("search?view=json",                 f"{base}/search?q={Q}&view=json"),
        ("search.json",                      f"{base}/search.json?q={Q}&type=product"),
        ("suggest.json",                     f"{base}/search/suggest.json?q={Q}&resources[type]=product&resources[limit]=20"),
        (f"col/{col}/p1",                    f"{base}/collections/{col}/products.json?limit=250&page=1"),
        ("col/all/p1",                       f"{base}/collections/all/products.json?limit=10&page=1"),
    ]

    print(f"\n=== Shopify: {store['id']} ({base}) ===")
    results = {}
    for label, url in endpoints:
        r = probe_url(sc, url)
        results[label] = r
        status = r.get("status", "ERR")
        ct = r.get("ct", "")[:40]
        pc = r.get("product_count", "?")
        nm = r.get("name_match_count", "?")
        cf = " [CLOUDFLARE]" if r.get("cloudflare") else ""
        link = f" Link={r.get('link_header','')[:60]}" if "link_header" in r else ""
        err = f" ERROR={r.get('error','')}" if "error" in r else ""
        first = f" first={r.get('first_title','')[:50]}" if "first_title" in r else ""
        print(f"  {label:<30} status={status} ct={ct:<40} products={pc} matches={nm}{cf}{link}{first}{err}")

    return results


def probe_tcgpro(store):
    base = store["url"]
    sid = store["id"]
    print(f"\n=== TCGPlayer Pro: {sid} ({base}) ===")

    results = {}
    search_url = f"{base}/search/products?productLineName=Magic%3A+The+Gathering&q={Q}"

    # Probe 1: GET the search page
    session = requests.Session()
    r_get = probe_url(None, search_url, session=session)
    results["GET_search_page"] = r_get
    print(f"  GET search page: status={r_get.get('status','ERR')} final_url={r_get.get('final_url','?')[:80]} cloudflare={r_get.get('cloudflare',False)}")
    if "body_preview" in r_get:
        print(f"    body_preview: {r_get['body_preview'][:150]}")

    # Probe 2: POST catalog search (reuse session to carry cookies)
    api_url = f"{base}/api/catalog/search"
    payload = {
        "query": QUERY,
        "context": {"productLineName": "Magic: The Gathering"},
        "filters": {},
        "from": 0,
        "size": 48,
        "sort": [{"field": "in-stock-price-sort", "order": "asc"}],
    }
    api_headers = {
        "Content-Type": "application/json",
        "Origin": base,
        "Referer": search_url,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    r_post = probe_url(None, api_url, method="POST", json_payload=payload, extra_headers=api_headers, session=session)
    results["POST_catalog_api"] = r_post
    print(f"  POST catalog API: status={r_post.get('status','ERR')} ct={r_post.get('ct','')[:50]} cloudflare={r_post.get('cloudflare',False)}")
    if "json_keys" in r_post:
        print(f"    json_keys: {r_post['json_keys']}")
    if "product_count" in r_post:
        print(f"    products={r_post['product_count']} matches={r_post.get('name_match_count','?')}")
    if "body_preview" in r_post:
        print(f"    body_preview: {r_post['body_preview'][:200]}")
    if "error" in r_post:
        print(f"    ERROR: {r_post['error']}")

    return results


def main():
    all_results = {"shopify": {}, "tcgpro": {}}

    for store in SHOPIFY_STORES:
        all_results["shopify"][store["id"]] = probe_shopify(store)

    for store in TCG_STORES:
        all_results["tcgpro"][store["id"]] = probe_tcgpro(store)

    # Save raw results
    with open("/home/user/nwa-mtg-finder/probe_results.json", "w") as f:
        json.dump(all_results, f, indent=2)
    print("\n\nRaw results saved to probe_results.json")

    # Quick summary
    print("\n\n=== SUMMARY ===")
    for sid, probes in all_results["shopify"].items():
        print(f"\n{sid}:")
        for label, r in probes.items():
            status = r.get("status", "ERR")
            pc = r.get("product_count", "?")
            nm = r.get("name_match_count", "?")
            err = r.get("error", "")
            cf = "[CF]" if r.get("cloudflare") else ""
            print(f"  {label:<30} {status} products={pc} matches={nm} {cf} {err[:60]}")

    for sid, probes in all_results["tcgpro"].items():
        print(f"\n{sid}:")
        for label, r in probes.items():
            status = r.get("status", "ERR")
            err = r.get("error", "")
            cf = "[CF]" if r.get("cloudflare") else ""
            print(f"  {label:<30} {status} {cf} {err[:60]}")


if __name__ == "__main__":
    main()
