# NWA MTG Finder — Project Context for Claude

## What this app does

Flask + Playwright web app that searches Magic: The Gathering singles inventory
across five Northwest Arkansas local game stores simultaneously.

- **Developer / user**: Chris (Moxfield: `mirkwoodrunner`)
- **Repo**: https://github.com/mirkwoodrunner/nwa-mtg-finder
- **Deployed**: Render (free tier, Docker Blueprint, auto-deploys on push to main)

---

## The Five Stores

| Key        | Name                      | Platform        | Base URL                                      | Collection Slug            |
|------------|---------------------------|-----------------|-----------------------------------------------|----------------------------|
| `finalboss`| Final Boss Games          | Shopify/BinderPOS | https://finalbossgames.com                  | `singles`                  |
| `gearbv`   | Gear Gaming Bentonville   | Shopify/BinderPOS | https://bentonville.geargamingstore.com     | `mtg-singles-all-products` |
| `gearfv`   | Gear Gaming Fayetteville  | Shopify/BinderPOS | https://fayetteville.geargamingstore.com    | `mtg-singles-all-products` |
| `chaos`    | Chaos Games               | TCGPlayer Pro   | https://chaosgamesnwa.tcgplayerpro.com        | n/a                        |
| `xxplo`    | Games Explosion           | TCGPlayer Pro   | https://gamesexxplosion.tcgplayerpro.com      | n/a                        |

---

## Architecture

### Search flow (two-phase UX)
1. User types card name → Scryfall lookup fires (validation + art display)
2. Five per-store buttons appear — each triggers an isolated `/api/search?q=&store=` request
3. Per-store sequential queries only — parallel fan-out causes cascading timeouts on Render free tier

### Key endpoints
- `GET /api/search?q=<card>&store=<key>` — single store search
- `GET /api/debug` — verbose probe of all three Shopify stores (hardcoded to "lightning bolt")
- `GET /health` — health check for Render

---

## Scraper: Shopify / BinderPOS (`search_shopify`)

### How it works
1. Tries up to three search endpoint paths (`/search?q=...&view=json` variants) — stops at the first that returns a valid JSON product list; paginates up to 5 pages (≤20 results/page)
2. Runs Shopify predictive search (`/search/suggest.json`) unconditionally for belt-and-suspenders coverage
3. Paginates the store's named collection in two passes — title-ascending (up to 20 pages × 250) and created-descending (up to 3 pages × 250) — to catch anything the search endpoint misses
4. Falls back to `_find_mtg_collection` (enumerates `/collections.json`) and then `/collections/all` if the named collection returns nothing
5. `parse_shopify` iterates **all variants** per product (not just the first) and filters by query string match on title
6. Deduplication via `seen_keys = set()` on (url, name) tuples throughout

### Bugs fixed
- **Early return bug**: `search_shopify` previously returned after the first page that had any match, truncating multi-page results. Now fully paginates.
- **Single variant bug**: `parse_shopify` previously captured only `variants[0]`. Now iterates all variants.

### Current status
- **Final Boss Games**: Returning partial results. Root cause not yet confirmed — may be collection pagination or search endpoint inconsistency.
- **Gear Gaming (both stores)**: Returning zero results. Root cause unknown — candidates are:
  - Collection slug `mtg-singles-all-products` may not match actual slug on their Shopify instance
  - Search endpoint may return HTML instead of JSON on these stores (some BinderPOS installs disable JSON view)
  - Cloudscraper may be getting rate-limited or blocked
- **Next step**: Run `/api/debug` against Gear stores and inspect status codes, content-type, and product counts before making any further changes.

---

## Scraper: TCGPlayer Pro (`search_tcgpro`)

### How it works
Two-tier approach — Playwright is the fallback, not the primary:

1. **Primary — direct HTTP POST** (`_search_tcgpro_http`): POSTs to `{store_url}/api/catalog/search` with a Magic: The Gathering context payload. Seeds the ASP.NET session cookie with a quick GET first. If this returns results, Playwright is never launched.
2. **Fallback — Playwright DOM scraping** (`_scrape_tcgpro_dom`): Headless Chromium navigates to the search URL; `stealth_async(page)` is applied before `goto`. Scrapes rendered product cards using a cascade of CSS selectors. No XHR interception — the current code does not use that approach.

Results from both paths are parsed by `_parse_tcgpro_products`, which walks any dict/list tree depth-first to find product-like item lists and filters by `name_matches`.

### There is NO XHR interception in the current codebase
Earlier versions of the scraper intercepted XHR responses. That approach was replaced entirely by the HTTP POST + DOM fallback architecture above. Any notes referencing XHR interception, `on_resp()`, `skip_keywords`, or wait loops are obsolete.

### Bugs resolved (by the rewrite)
- Silent outer exception handler now logs in both the HTTP path and the Playwright path
- `skip_keywords` URL-matching bug is N/A (no XHR interception)
- Name-field gate iterates the full candidate list in `_parse_tcgpro_products` (not just index 0)
- Wait-loop early-exit bug is N/A (no XHR interception)

### Current status
- **Chaos Games / Games Explosion**: HTTP POST path may fail if the store requires an authenticated session or returns a non-200 for anonymous requests. Playwright DOM fallback fires in that case.
- **Next step**: Run a curl test against `/api/search?q=lightning+bolt&store=chaos` and `&store=xxplo` to confirm the HTTP POST path succeeds, then check if DOM fallback is needed.

---

## Moxfield Integration

Do NOT attempt live API calls to Moxfield — Cloudflare blocks Render datacenter IPs,
and CORS blocks client-side calls. The only viable approach is deep links:

```
https://www.moxfield.com/search#q=<CARD_NAME>&authorUsernames=mirkwoodrunner
```

---

## Workflow

- **Planning / diagnosis**: Claude.ai (this context)
- **Implementation**: Claude Code (reads full codebase, pushes to GitHub)
- **Deployment**: Render auto-deploys on push to `main`
- **All code changes go through Claude Code** — no zip uploads, no inline patches

### Instruction format for Claude Code
Task descriptions are written as standalone markdown files with explicit find/replace patches
or clear implementation instructions — not prose explanations.

---

## Key principles

- **Audit before fixing, test after fixing** — broad audit → focused re-audit on real functions → targeted fix → curl verification
- **Phantom function risk** — code audits can hallucinate functions not in the codebase; re-audits must reference verbatim code
- **Silent failures are the primary enemy** — missing logging on exception handlers repeatedly obscures root causes
- **Don't fight Cloudflare** — multiple approaches failed; deep links are the correct solution
- **Shopify pagination must be full** — early-return patterns silently truncate real inventory
