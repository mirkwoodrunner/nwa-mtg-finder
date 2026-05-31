# Store Search Investigation — Findings

_Investigated: 2026-05-31. Read-only. No changes made to app.py or store-facing files._

---

## Probe Results

### Environment note

All probes were run from a managed cloud execution environment (this Claude Code session container).
**Every single endpoint across all five stores returned HTTP 403 with body `Host not in allowlist`.**
This is a datacenter IP block — the stores reject requests from cloud/hosting IP ranges at the
reverse-proxy level. It is NOT a Cloudflare CAPTCHA challenge (the word "cloudflare" does not appear
in any response body). The block is consistent across both Shopify and TCGPlayer Pro stores.

This means probe results from this environment cannot confirm what Render's production server sees.
Per CLAUDE.md, Final Boss Games does return partial results from Render, which indicates Render's IPs
are NOT in the same blocklist as this container's IPs. Gear Gaming and TCGPlayer Pro stores may or
may not be blocked from Render — that must be verified from a Render shell or from the `/api/debug`
endpoint on the deployed instance.

### Shopify Stores

| Store     | Endpoint                              | Status | Content-Type | Products | Matches | Notes                    |
|-----------|---------------------------------------|--------|-------------|----------|---------|--------------------------|
| finalboss | GET /search?q=...&type=product&view=json | 403 | text/plain   | —        | —       | "Host not in allowlist"  |
| finalboss | GET /search?q=...&view=json           | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| finalboss | GET /search.json?q=...                | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| finalboss | GET /search/suggest.json              | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| finalboss | GET /collections/singles/products.json | 403   | text/plain   | —        | —       | "Host not in allowlist"  |
| finalboss | GET /collections/all/products.json    | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| gearbv    | GET /search?q=...&type=product&view=json | 403 | text/plain   | —        | —       | "Host not in allowlist"  |
| gearbv    | GET /search?q=...&view=json           | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| gearbv    | GET /search.json?q=...                | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| gearbv    | GET /search/suggest.json              | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| gearbv    | GET /collections/mtg-singles-all-products/products.json | 403 | text/plain | — | — | "Host not in allowlist" |
| gearbv    | GET /collections/all/products.json    | 403    | text/plain   | —        | —       | "Host not in allowlist"  |
| gearfv    | (all 6 endpoints)                     | 403    | text/plain   | —        | —       | "Host not in allowlist"  |

No Link headers were returned on any collection endpoint (all blocked before reaching Shopify).

### TCGPlayer Pro Stores

| Store | Probe                       | Status | Body / Result                  |
|-------|-----------------------------|--------|-------------------------------|
| chaos | GET /search/products?...    | 403    | "Host not in allowlist"        |
| chaos | POST /api/catalog/search    | 403    | "Host not in allowlist"        |
| xxplo | GET /search/products?...    | 403    | "Host not in allowlist"        |
| xxplo | POST /api/catalog/search    | 403    | "Host not in allowlist"        |

---

## Code Analysis Findings

### Finding 1 — MEDIUM: `search_endpoint_worked` set True on empty product pages (app.py lines 143–148)

```python
add_results(parse_shopify(products, store["url"], query))
search_endpoint_worked = True              # line 144 — fires even when products = []
if len(products) < 20:
    break  # last page
if search_endpoint_worked:
    break  # outer loop — skips remaining path templates
```

When a search endpoint returns valid JSON with an empty `products` list (e.g., `{"products": []}`),
`search_endpoint_worked` is set to `True` and the outer loop breaks, preventing the next path
template from being tried. The remaining two path templates (`/search?q=...&view=json`,
`/search.json?q=...`) are skipped even though one of them might work. **The flag should only be set
when `len(products) > 0`**, i.e., the endpoint actually returned at least one product.

---

### Finding 2 — MEDIUM: TCGPlayer HTTP empty-list prevents Playwright fallback (app.py line 317)

```python
results = _search_tcgpro_http(store, query)
if results is not None:       # line 317 — [] is not None
    return (results, None)    # returns immediately; Playwright never tried
```

`_search_tcgpro_http` returns `[]` (an empty list, not `None`) when the POST returns HTTP 200 but
the response contains no products matching the query. An empty list is not `None`, so the `if
results is not None` guard passes and the function returns `([], None)` without invoking Playwright.

This means: if the TCGPlayer Pro catalog API answers 200 OK but its search index has no results for
this query (possible due to search relevance tuning, store-side data gaps, or the catalog API
returning a different product shape than `_parse_tcgpro_products` recognises), Playwright is
silently skipped even though the JS-rendered page may display the card correctly.

**The guard should be `if results` (truthy check), not `results is not None`.**

---

### Finding 3 — LOW: `parse_shopify` skips variants where `available` key is absent (app.py line 50)

```python
for v in variants:
    if not v.get("available"):    # line 50 — also False when key is missing
        continue
```

`v.get("available")` returns `None` when the `available` key is not present in the variant dict.
`None` is falsy, so the variant is skipped as if it were out of stock. Shopify's storefront JSON
guarantees the `available` field, but BinderPOS custom themes may not always include it on every
variant. Any variant missing the key is silently excluded. The check should be
`if v.get("available") is False` or the field should be treated as absent-means-assume-available.

---

### Finding 4 — LOW: `_parse_tcgpro_products` `has_name` gate checks only first 5 items (app.py line 228)

```python
has_name = any(any(k in item for k in name_keys) for item in candidate_list[:5])
if not has_name:
    continue
```

If the first 5 items in a candidate list are non-product metadata entries (pagination tokens,
facets, totals) without name keys, the entire list is rejected even if actual product objects appear
at index 5 or later. This is an edge case but is structurally fragile for any API that prepends
metadata to its product arrays.

---

### Finding 5 — LOW: `quantity=0` in TCGPlayer items defaults to `1`, treated as in-stock (app.py line 241)

```python
qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
```

`0` is falsy. An item with `"quantity": 0` falls through the `or` chain and is assigned `qty = 1`.
The subsequent `if int(str(qty).split(".")[0]) <= 0: continue` check then sees `1` and does NOT
skip the item. A card with zero quantity is shown as available.

---

### Finding 6 — MEDIUM: `name_matches` substring shortcut matches inside longer words (app.py line 36)

```python
def name_matches(title, query):
    tl = title.lower()
    ql = query.lower()
    if ql in tl:      # line 36 — raw substring, no word boundary
        return True
    words = ql.split()
    return all(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in words) if words else False
```

The first check `if ql in tl` is a raw substring match with no word-boundary enforcement.
`name_matches("Livestock", "Stock")` returns `True` because `"stock"` is a substring of `"livestock"`.
The word-boundary regex in the fallback path is never reached because the substring check fires first.
This means queries for common short words ("Stock", "Bolt", "Pit") can produce false positives against
cards that contain those strings within longer words.

**Confirmed by test failure:** `test_name_matches_word_boundary_no_false_positive` fails.

---

### Finding 7 — LOW: `name_matches` returns True for empty query (app.py line 36)

```python
if ql in tl:   # "" in any_string is always True in Python
    return True
```

An empty string is a substring of every string in Python. `name_matches("Lightning Bolt", "")` returns
`True`. The fallback path's `if words else False` would correctly return `False` for an empty query
(`words = [].split()` = `[]`), but it is never reached because the substring check fires first.
This is a minor issue since `api_search` validates non-empty queries before calling any scraper, but
it means any internal caller that passes an empty query will get spurious matches.

**Confirmed by test failure:** `test_name_matches_empty_query_returns_false` fails.

---

### Finding 8 — LOW: `get_json_with_retry` catches exceptions silently (app.py lines 83–85)

```python
except Exception:
    if attempt < retries:
        time.sleep(1)
    continue
return None
```

Network errors (connection refused, SSL error, DNS failure, timeout) are swallowed with no log
output. The caller receives `None` and proceeds silently as if the endpoint was absent rather than
erroring. On Render, a transient network blip looks identical to "this endpoint path doesn't exist."

---

## Discrepancies with AUDIT.md

### 1. AUDIT.md §2 (items 5, 6, 8, 9) describes XHR interception architecture that does not exist

AUDIT.md describes `skip_keywords`, `on_resp`, `intercepted`, and a 12-iteration wait loop as
sources of failure. None of these constructs exist anywhere in `app.py`. The live code uses an HTTP
POST primary path (`_search_tcgpro_http`) and a DOM-scraping Playwright fallback
(`_scrape_tcgpro_dom`). AUDIT.md was written against an older version of the codebase. Items 5, 6,
8, and 9 in AUDIT.md's summary table are entirely obsolete.

### 2. AUDIT.md item 2: dedup by name only — incorrect

AUDIT.md describes `seen_names = set()` with dedup keyed on bare card name. Live code (line
237–239) uses `seen_ids = set()` keyed on `productId`, `id`, or name (in that priority order).
Multiple conditions/printings of the same card would only collapse if they share the same `id` with
no `productId`.

### 3. AUDIT.md item 3: `name_matches` filters short words — incorrect

AUDIT.md describes `words = [w for w in ql.split() if len(w) > 2]`. Live code (line 38) is
`words = ql.split()` — no short-word filter exists. Two-letter words like "of", "or", "by" are
included in the word-boundary regex check, not dropped.

### 4. AUDIT.md item 7: silent Playwright exception handler — partially inaccurate

AUDIT.md says the outer `except Exception` returns `([], None)` with no logging. Live code (line
344) calls `traceback.print_exc()` before returning `([], str(e))`. The error is logged and the
error string is passed back to the caller. However, `api_search` (line 471) only surfaces the error
if `not results`: `"error": err if not results else None` — so it does appear in the response when
results are empty.

### 5. AUDIT.md item 4 / item 18: `err` always `None` — partially inaccurate

AUDIT.md says both `search_shopify` and `search_tcgpro` always return `None` as the error slot.
`search_shopify` (line 205) still always returns `None` as the second element. But `search_tcgpro`
(line 347) returns `str(e)` on the Playwright exception path, and `None` otherwise. The Shopify
side is still dead, but the TCGPlayer side can surface a real error message when Playwright crashes.

### 6. AUDIT.md line number references are systematically wrong

AUDIT.md references line numbers ~208, ~234, ~263, ~278, etc. The live codebase has those
constructs at completely different locations or not at all. All line number citations in AUDIT.md
should be treated as approximate-to-obsolete.

---

## Recommended Fix Priority

1. **Verify from Render that stores are actually reachable** — before any code fixes, hit
   `/api/debug` on the live Render deployment with `?q=lightning+bolt` and record the raw probe
   results. All probes from this environment return 403 (cloud IP block), so every hypothesis about
   store behavior must be confirmed from Render's IP.

2. **Fix `search_endpoint_worked` false positive (Finding 1, app.py line 144)** — change `search_endpoint_worked = True` to fire only when `len(products) > 0`. This is a one-line fix with direct impact on any store where the first search path returns valid JSON with zero products.

3. **Fix TCGPlayer empty-list Playwright bypass (Finding 2, app.py line 317)** — change `if results is not None:` to `if results:`. This ensures Playwright is tried whenever the HTTP POST returns zero results, not just when it errors.

4. **Fix `quantity=0` treated as in-stock (Finding 5, app.py line 241)** — replace the `or` chain with explicit `is not None` checks. Confirmed bug, low effort.

5. **Fix `name_matches` substring false positives (Finding 6, app.py line 36)** — remove or replace the `if ql in tl: return True` shortcut with a whole-word check. The two failing unit tests (`test_name_matches_word_boundary_no_false_positive`, `test_name_matches_empty_query_returns_false`) document the required correct behavior.

6. **Investigate `parse_shopify` variant `available` key (Finding 3, app.py line 50)** — check what BinderPOS actually returns for the `available` field on variants. If the key is consistently present, no fix needed. If absent on some variants, change `if not v.get("available")` to `if v.get("available") is False`.

7. **Update AUDIT.md to reflect the current architecture** — the XHR interception section (§2) is entirely wrong and will mislead future debugging.
