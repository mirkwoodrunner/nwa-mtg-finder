# NWA MTG Local Store Finder — Claude Code Context

## What this app does
Flask + Playwright web app that lets a user search Magic: The Gathering singles
inventory across five Northwest Arkansas game stores simultaneously.

## Deployment
- **Platform**: Render (free tier, Docker Blueprint)
- **Auto-deploy**: Every push to `main` triggers a Render rebuild (~5–10 min)
- **Live URL**: https://nwa-mtg-finder.onrender.com
- **Health check**: https://nwa-mtg-finder.onrender.com/health
- **Debug endpoint**: https://nwa-mtg-finder.onrender.com/api/debug?q=lightning+bolt

## Key files
```
app.py              — Flask backend, all scraping logic
static/index.html   — Single-page frontend (vanilla JS, no build step)
Dockerfile          — Uses mcr.microsoft.com/playwright/python:v1.50.0-jammy
render.yaml         — Render Docker Blueprint config
requirements.txt    — Python deps
CLAUDE.md           — This file
```

## The five stores

| Key        | Name                      | Platform         | Collection key            |
|------------|---------------------------|------------------|---------------------------|
| finalboss  | Final Boss Games          | Shopify/BinderPOS | singles                  |
| gearbv     | Gear Gaming Bentonville   | Shopify/BinderPOS | mtg-singles-all-products |
| gearfv     | Gear Gaming Fayetteville  | Shopify/BinderPOS | mtg-singles-all-products |
| chaos      | Chaos Games               | TCGPlayer Pro    | —                         |
| xxplo      | Games Explosion           | TCGPlayer Pro    | —                         |

## Architecture decisions (do not change without good reason)

**Sequential per-store queries** — Parallel fan-out to all 5 stores causes
cascading timeouts on Render's free tier. The two-phase UX (Scryfall lookup
first, then individual per-store buttons) is intentional.

**No Moxfield API calls** — Cloudflare blocks Render's datacenter IPs server-side,
and CORS blocks client-side calls. Use deep links only:
`https://www.moxfield.com/wants?username=mirkwoodrunner&q=<CARD_NAME>`

**Single gunicorn worker** — Free tier constraint. TCGPlayer searches run in a
new asyncio event loop per request (sync Flask route → `asyncio.new_event_loop()`).

## Scraper notes

### Shopify/BinderPOS
- Two phases: search endpoint first, then collection pagination (up to 8 pages × 250)
- `get_json_with_retry()` retries twice with 1s backoff (handles cold-start 503s)
- `name_matches()` does fuzzy all-words match, not just substring
- `parse_shopify()` iterates ALL variants per product (not just first)
- Deduplication via `seen_keys = set()` on (url, name) tuples

### TCGPlayer Pro (Playwright)
- Intercepts XHR responses from the store's React SPA
- `playwright-stealth` applied via `stealth_async(page)` before every navigation
- XHR interception uses a blocklist (analytics/tracking URLs blocked, rest passes)
- Processes ALL intercepted batches, not just the first
- 12-second wait loop for XHR to arrive
- DOM fallback: if XHR yields nothing, scrapes rendered product cards directly
- `_extract_tcg_price()` and `_build_tcgpro_url()` are separate helpers

## Debugging zero results

1. Hit the debug endpoint for Shopify stores:
   `https://nwa-mtg-finder.onrender.com/api/debug?q=<CARD_NAME>`
   It reports: HTTP status, content-type, product count, query match count, sample title.

2. For TCGPlayer stores, add temporary logging to `on_resp()` in `search_tcgpro()`
   to print intercepted URLs and response shapes.

3. Common failure modes:
   - Shopify 403 → cloudscraper needs update or store added Cloudflare
   - Shopify returns HTML instead of JSON → check content-type in debug output
   - TCGPlayer XHR intercept empty → SPA may have changed API URL patterns
   - TCGPlayer DOM fallback needed → check if selectors in `_scrape_tcgpro_dom()` match

## Git workflow
- Repo: https://github.com/mirkwoodrunner/nwa-mtg-finder
- Push directly to `main` — Render auto-deploys on every push

## Owner
Chris — sole developer. Moxfield username: `mirkwoodrunner`
