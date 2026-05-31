"""
Standalone test: can Playwright + stealth bypass the Shopify 403s?
Run with: python test_shopify_playwright.py
Prints a result table. Does not modify any app file.
"""
import asyncio, json, re
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STORES = [
    {
        "id": "finalboss",
        "base": "https://finalbossgames.com",
        "col": "singles",
    },
    {
        "id": "gearbv",
        "base": "https://bentonville.geargamingstore.com",
        "col": "mtg-singles-all-products",
    },
    {
        "id": "gearfv",
        "base": "https://fayetteville.geargamingstore.com",
        "col": "mtg-singles-all-products",
    },
]

QUERY = "lightning bolt"
Q = "lightning+bolt"


async def try_endpoint(page, url, label):
    """Navigate to url, return (status, content_type, product_count, first_title)."""
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        status = resp.status if resp else 0
        ct = (resp.headers.get("content-type", "") if resp else "")
        body = await page.content()

        # page.content() returns the DOM — for JSON endpoints Playwright wraps it in <html><body><pre>
        # Extract the raw text from inside <pre> if present, else use body directly
        pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.DOTALL)
        raw = pre_match.group(1) if pre_match else body

        # Try parsing as JSON
        try:
            data = json.loads(raw)
        except Exception:
            return status, ct, 0, f"non-JSON (body starts: {raw[:80].strip()!r})"

        # Extract products from various shapes
        if "resources" in data:
            products = data.get("resources", {}).get("results", {}).get("products", [])
        else:
            products = data.get("products") or data.get("results") or []

        count = len(products) if isinstance(products, list) else 0
        first = products[0].get("title", "?")[:60] if count > 0 else ""
        return status, ct, count, first

    except Exception as e:
        return 0, "", 0, f"ERROR: {e}"


async def probe_store(store):
    print(f"\n{'='*60}")
    print(f"Store: {store['id']}  ({store['base']})")
    print(f"{'='*60}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--ignore-certificate-errors",
            ],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            ignore_https_errors=True,
        )
        page = await context.new_page()
        await stealth_async(page)

        # Warm up: visit the store homepage first to get cookies / pass any JS challenge
        print(f"  Warming up: GET {store['base']} ...")
        try:
            warm = await page.goto(store["base"], wait_until="networkidle", timeout=25000)
            print(f"  Warmup status: {warm.status if warm else 'no response'}")
        except Exception as e:
            print(f"  Warmup failed: {e}")

        endpoints = [
            ("search_json",  f"{store['base']}/search?q={Q}&type=product&view=json"),
            ("search_plain", f"{store['base']}/search?q={Q}&view=json"),
            ("predictive",   f"{store['base']}/search/suggest.json?q={Q}&resources[type]=product&resources[limit]=20"),
            ("col_page1",    f"{store['base']}/collections/{store['col']}/products.json?limit=250&page=1"),
            ("all_page1",    f"{store['base']}/collections/all/products.json?limit=10&page=1"),
        ]

        any_products = False
        for label, url in endpoints:
            status, ct, count, info = await try_endpoint(page, url, label)
            flag = "✓" if count > 0 else ("?" if status == 200 else "✗")
            print(f"  [{flag}] {label}: status={status} products={count}  {info}")
            if count > 0:
                any_products = True

        await browser.close()
        return any_products


async def main():
    results = {}
    for store in STORES:
        ok = await probe_store(store)
        results[store["id"]] = ok

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for sid, ok in results.items():
        print(f"  {sid}: {'PRODUCTS FOUND — Playwright works' if ok else 'STILL BLOCKED or zero products'}")

    if any(results.values()):
        print("\nRESULT: At least one store responded. Proceed with app.py patch.")
    else:
        print("\nRESULT: All stores still blocked. Do NOT patch app.py. Report findings.")


asyncio.run(main())
