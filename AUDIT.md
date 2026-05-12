# NWA MTG Finder — Code Audit Report

_Audited: 2026-05-12. Read-only — no changes made._

---

## 1. Correctness Bugs

---

### [app.py ~284] — MEDIUM — TCGPlayer `quantity=0` not treated as out-of-stock

```python
qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
```

`0` is falsy in Python. If an item has `"quantity": 0`, the `or` chain skips it and falls through to
the next field or defaults to `1`. The subsequent `if int(...) <= 0: continue` then sees `1` and does
NOT skip the item. A card with zero stock appears as available.

**Correct behavior:** use `item.get("quantity") if item.get("quantity") is not None else ...` or
check all fields before defaulting.

---

### [app.py ~278–280] — MEDIUM — TCGPlayer dedup by name only collapses multiple conditions/printings

```python
seen_names = set()
...
if name in seen_names:
    continue
seen_names.add(name)
```

Multiple XHR batches may contain the same card in different conditions (NM, LP, HP) or different
printings. Deduplication on bare card name means only the first encountered listing survives. A
cheaper LP copy intercepted in a second XHR batch is silently dropped.

**Correct behavior:** deduplicate on `(name, condition, set)` or allow all listings through.

---

### [app.py ~32–39] — LOW — `name_matches` short-word filtering is inconsistent

```python
if ql in tl:
    return True
words = [w for w in ql.split() if len(w) > 2]
return all(w in tl for w in words) if words else False
```

Two-letter words like "or", "by", "of" are stripped from the `words` list. If a query consists
entirely of short words, `words = []` and the `if words else False` branch returns `False`. The only
match path is the substring check at line 36. This is inconsistent: some queries match too broadly
(substring anywhere), others too narrowly.

**Correct behavior:** document and unify the matching contract. `if words else False` probably should
be `True` for empty `words`, or the substring check should be made exact.

---

### [app.py ~425] — MEDIUM — `err` is always `None`; real failures return `error: null` to frontend

Both `search_shopify` (`return (all_results, None)`) and `search_tcgpro` (`return (results, None)`)
always produce `None` as the second tuple element. `api_search` then does:

```python
"error": err if not results else None
```

This is always `None`. A Playwright crash, a `cloudscraper` 403, or any other failure silently
returns `{"results": [], "error": null}`. The frontend shows "Nothing in stock" rather than "Error."

**Correct behavior:** surface real exception messages as the error string.

---

## 2. TCGPlayer Pro Scraper — Why `chaos` and `xxplo` Are Likely Failing

---

### [app.py ~208–210] — MEDIUM — `skip_keywords` substring check can block legitimate API URLs

```python
skip_keywords = ["analytics", "telemetry", "tracking", "gtm", "segment",
                 "hotjar", "sentry", "favicon", "font", "css"]
if any(k in url.lower() for k in skip_keywords):
    return
```

`k in url.lower()` is a substring match on the **full URL including query parameters**. A legitimate
TCGPlayer catalog API call like:

```
https://catalog.tcgplayer.com/v3/search?q=bolt&trackingSource=organic
```

contains `"tracking"` in the query string and is silently blocked. TCGPlayer's SPA commonly appends
analytics or A/B test parameters to API calls. This causes all results to be silently dropped with
no indication of why.

**Correct behavior:** restrict skip_keywords checks to the URL path only (before `?`), or use
more-specific hostname patterns (e.g., `hotjar.com`, `sentry.io`).

---

### [app.py ~234–238] — CRITICAL — Name-field gate rejects inventory/pricing XHR batches

```python
if not isinstance(cands[0], dict):
    return
if any(k in cands[0] for k in ["name","productName","cleanName","title",
                                 "productTitle","cardName"]):
    intercepted.append((url, cands))
```

TCGPlayer Pro's SPA makes at least two types of XHR calls:

1. **Catalog/search** — returns product objects with `name`, `productId`, set info, but often no
   pricing.
2. **Inventory/pricing** — returns objects with `productConditionId`, `price`, `quantity`,
   `productId` — **no name field**.

If only the pricing XHR is intercepted (catalog was cached, came from a CDN, or fired before the
handler was installed), `cands[0]` has no name key and the entire batch is rejected. Zero results,
no error. This is almost certainly the primary failure mode for `chaos` and `xxplo`.

**Correct behavior:** accept inventory batches that have a price and a `productId`; join against
previously-intercepted catalog data, or stop requiring a name key to accept a batch.

---

### [app.py ~263] — CRITICAL — Top-level `except Exception` silently swallows all Playwright failures

```python
except Exception:
    return ([], None)
```

Any error in the async block — browser launch failure, Playwright crash, stealth initialization
error, bot-detection redirect, async task exception — is silently converted to empty results. There
is no logging, no traceback, and `err` is `None`. On Render, if Chromium can't launch or TCGPlayer
serves a Cloudflare challenge, the operator has no way to know.

**Correct behavior:** log the exception (at minimum `traceback.print_exc()`), and return
`([], str(e))` so the frontend can show "Error" instead of "Not found."

---

### [app.py ~246–249] — MEDIUM — Wait loop breaks on first intercepted batch, missing subsequent XHR calls

```python
for _ in range(12):
    await page.wait_for_timeout(1000)
    if intercepted:
        break
```

The loop exits as soon as any batch is captured. After `break`, the code immediately calls
`await browser.close()`. Any XHR calls that arrive in the next few hundred milliseconds (e.g., a
second pagination call, a separate pricing API call) are abandoned. The comment in CLAUDE.md notes
"processes ALL intercepted batches" — this is true only for batches that arrive *before* the break
fires.

**Correct behavior:** instead of `break`, wait a fixed additional grace period after first intercept
(e.g., 2 more seconds) before closing, to allow in-flight companion calls to complete.

---

### [app.py ~239] — MEDIUM — `on_resp` exceptions silently swallowed

```python
except Exception:
    pass
```

Errors inside the async response handler (malformed JSON, body unavailable, connection dropped) are
suppressed entirely. Combined with the issue above, a complete XHR parse failure gives the same
external appearance as "no relevant XHR."

**Correct behavior:** at minimum, log to stderr with enough context (response URL, error type) to
aid debugging.

---

## 3. Shopify Scraper — Recent Fixes and Remaining Edge Cases

The three named fixes are correctly implemented:

- `parse_shopify:49` iterates `for v in variants` (all variants).
- No early return on first matching product.
- `seen_keys` on `(url, name)` in `search_shopify:113–116` deduplicates across search + collection
  results.

---

### [app.py ~48] — LOW — `variants or [{}]` creates a phantom empty dict for products with no variants

```python
variants = p.get("variants") or [{}]
```

If a Shopify product has `"variants": []`, `[] or [{}]` returns `[{}]` — a list with one empty
dict. Iterating it checks `{}.get("available")` which is `None` (falsy), so `continue` fires and
nothing is added. Functionally correct, but the substitution `[{}]` is misleading; `or []` would
more clearly express "skip if no variants."

---

### [app.py ~137] — LOW — Pagination terminates on `len(products) < 20` which could cut short on full last pages

```python
if len(products) < 20:
    break
```

This assumes exactly 20 per full page and exits early if fewer arrive. If a BinderPOS configuration
uses a different page size and returns exactly 20 items on the last page, the iteration exits early
and misses any remaining pages.

---

## 4. Error Handling — Silent Failures

| Location | Severity | Description |
|---|---|---|
| `app.py:82–85` | Medium | `get_json_with_retry` catches all exceptions with no logging. Network errors, SSL failures, and timeouts are invisible. |
| `app.py:100–101` | Low | `_find_mtg_collection` swallows all exceptions, silently returning `None`. |
| `app.py:239` | Medium | `on_resp` inner exception handler is bare `pass` — see §2. |
| `app.py:263` | Critical | Top-level `search_tcgpro` exception returns `([], None)` — see §2. |
| `app.py:250–251` | Medium | `page.goto` exception is silently swallowed; no distinction between "page load failed" and "page loaded but no XHR." |
| `app.py:425` | Medium | `err` is always `None`; real failures never reach the frontend error field. |

---

## 5. Timeout / Performance Risks

---

### [app.py ~183–260] — CRITICAL — Worst-case TCG request blocks the single gunicorn worker for ~40 seconds

Per-request timeline for a TCG store:

| Step | Max time |
|---|---|
| Playwright browser launch | ~2–5s |
| `page.goto(..., timeout=25000)` | 25s |
| Wait loop (`12 × 1000ms`) | 12s |
| DOM fallback `wait_for_timeout(3000)` | 3s |
| **Total worst case** | **~40s** |

With `--workers 1`, any second request (Shopify or TCG) queues behind this. The gunicorn
`--timeout 180` accommodates it, but a user's browser connection typically times out at ~60s.

---

### [Dockerfile:24 / app.py ~186] — LOW — `--single-process` Chromium flag causes instability under memory pressure

The `search_tcgpro` function passes `--single-process` to Chromium. This collapses
renderer/browser/GPU into one OS process to save memory, but it is an unsupported Chromium mode
that can produce random crashes under memory pressure on Render's free tier (512 MB RAM). A crash
inside the browser hits the outer `except Exception` and silently returns `([], None)`.

---

### [app.py ~415] — LOW — `asyncio.set_event_loop(loop)` is not thread-safe

```python
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
```

This sets the process-global default event loop. With a single worker and no threading this is
benign. If `--threads` is ever added to gunicorn, concurrent TCG searches would race on the global
loop reference.

---

## 6. Security / Robustness

---

### [app.py:14] — LOW — CORS allows all origins

`CORS(app)` with no `origins=` restriction means any website can call this Flask API as a
cross-origin proxy. Not a vulnerability for a public read-only tool, but worth noting if the app
ever handles user state.

---

### [app.py:404] — LOW — No query length validation

`query = request.args.get("q","").strip()` is passed directly to all scraping functions with no
maximum length. An extremely long query would be passed verbatim to cloudscraper/Playwright.

---

### [index.html:285–287] — LOW — History entries rendered without HTML escaping

```js
h.map(q => `<button class="hist-chip" onclick="histSearch(event,${JSON.stringify(q)})">
  ${q}...
</button>`)
```

`JSON.stringify` makes the `onclick` attribute safe. But `${q}` in the button text is raw `innerHTML`.
A search query containing `<img src=x onerror=alert(1)>` would be saved to `localStorage` and
executed on re-render. History is same-origin only so real-world exploitability is extremely low,
but it is an `innerHTML`-injection antipattern.

---

## 7. Dead / Unreachable Code

---

### [app.py:172, 299–302, 425] — MEDIUM — The `err` return slot is always `None`; the conditional in `api_search` is dead

```python
# search_shopify
return (all_results, None) if all_results else ([], None)

# search_tcgpro
return (results, None) if results else ([], None)

# api_search
"error": err if not results else None   # always evaluates to None
```

Neither function ever populates the error slot. The `err if not results else None` expression always
produces `None`. The slot exists but is structurally dead.

---

### [app.py:340–344] — LOW — Slug-based URL construction in `_build_tcgpro_url` produces non-functional URLs

```python
pl_slug = product_line.lower().replace(" ","_").replace(":","")
return f"{store['url']}/product/{pl_slug}/{slug.lstrip('/')}"
```

The path `/product/magic_the_gathering/<slug>` is a guess and does not match TCGPlayer Pro's actual
URL structure. Any result that reaches this code path generates a broken link pointing to a 404.

---

## 8. Frontend Issues

---

### [index.html:392–394, 456] — OK — Moxfield deep link format is correct

The `/wants?username=...&q=...` format matches the CLAUDE.md specification. No bug.

---

### [index.html:509–512] — MEDIUM — In-flight fetch from previous search can overwrite `storeState` after a new search starts

```js
storeState = {};   // cleared on new doSearch()
...
// later, old fetch resolves:
storeState[storeId] = { status:'done', results:data.results||[], ... };
renderStoreBtns();
showStoreResults(storeId, storeState[storeId]);
```

There is no cancellation token or query-version check. If the user searches "Ragavan" → clicks
"Final Boss" → then immediately searches "Thoughtseize", the "Final Boss" fetch for Ragavan
completes and writes Ragavan results into `storeState`, then calls `showStoreResults` while
`currentQuery` is now "Thoughtseize". The results panel briefly shows the wrong card's inventory.

**Correct behavior:** stamp each search with a version counter and discard fetch results if the
counter has advanced.

---

### [index.html:514–519] — LOW — `currentMinPrice` is global across all previously searched stores

```js
Object.values(storeState).forEach(st => {
  (st.results||[]).forEach(r => {
    if (r.price && (currentMinPrice===null || r.price < currentMinPrice)) currentMinPrice = r.price;
  });
});
```

After Store A ($2.00) and Store B ($1.50) are both searched, displaying Store A's results shows
$2.00 without the green "best" highlight because the global min is $1.50 (from Store B, which isn't
currently visible). The "best" indicator becomes effectively unusable once more than one store has
been searched.

---

### [index.html:631] — LOW — Enter key during a pending Scryfall lookup triggers concurrent `doSearch` calls

```js
document.getElementById('q').addEventListener('keydown', e => {
  if(e.key==='Enter'&&acIndex<0) doSearch();
});
```

`doSearch` disables the search button but does not guard the keydown listener. Pressing Enter twice
quickly starts two concurrent `doSearch` executions racing over `currentQuery`, `storeState`, and
card panel rendering.

---

## Summary Table

| # | File / Line | Severity | Category | Issue |
|---|---|---|---|---|
| 1 | app.py:284 | Medium | Correctness | `quantity=0` not detected as out-of-stock |
| 2 | app.py:278 | Medium | Correctness | TCGPlayer dedup by name only; collapses conditions |
| 3 | app.py:38 | Low | Correctness | `name_matches` short-word filtering inconsistent |
| 4 | app.py:425 | Medium | Correctness | `err` always `None`; failures silently show as empty |
| 5 | app.py:208 | Medium | TCGPlayer | `skip_keywords` substring match may block API calls with tracking params |
| 6 | app.py:234 | **Critical** | TCGPlayer | Name-field gate rejects inventory/pricing XHR batches (most likely cause of zero results) |
| 7 | app.py:263 | **Critical** | TCGPlayer | Top-level `except` swallows all Playwright failures silently |
| 8 | app.py:246 | Medium | TCGPlayer | Wait loop breaks on first batch; later XHR calls abandoned |
| 9 | app.py:239 | Medium | TCGPlayer | `on_resp` exceptions silently swallowed |
| 10 | app.py:48 | Low | Shopify | `variants or [{}]` misleading; should be `or []` |
| 11 | app.py:137 | Low | Shopify | `< 20` page termination slightly brittle |
| 12 | app.py:82 | Medium | Error handling | `get_json_with_retry` logs nothing on exception |
| 13 | app.py:183 | **Critical** | Performance | Single worker blocked up to ~40s per TCG request |
| 14 | app.py:186 | Low | Performance | `--single-process` Chromium flag causes random crashes under memory pressure |
| 15 | app.py:415 | Low | Performance | `asyncio.set_event_loop` not thread-safe if workers ever scaled |
| 16 | app.py:14 | Low | Security | CORS open to all origins |
| 17 | app.py:404 | Low | Security | No query length limit |
| 18 | app.py:172,299,425 | Medium | Dead code | `err` return slot is structurally dead; conditional always `None` |
| 19 | app.py:340 | Low | Dead code | Slug-based URL in `_build_tcgpro_url` generates broken links |
| 20 | index.html:392 | — | Frontend | Moxfield link format is **correct** |
| 21 | index.html:509 | Medium | Frontend | Stale fetch overwrites `storeState` after new search begins |
| 22 | index.html:514 | Low | Frontend | `currentMinPrice` is global; "best" highlight breaks after multiple stores searched |
| 23 | index.html:285 | Low | Frontend | History entries not HTML-escaped in `innerHTML` |
| 24 | index.html:631 | Low | Frontend | Enter key during pending search triggers concurrent `doSearch` |
