# `search_tcgpro` Audit Feedback

## 1. XHR acceptance conditions (`on_resp`)

Five sequential filters must all pass before a batch is accepted:

1. URL contains `"tcgplayer"` (case-insensitive)
2. HTTP status is exactly `200`
3. `Content-Type` header contains `"json"`
4. URL does **not** contain any skip keyword:
   `analytics`, `telemetry`, `tracking`, `gtm`, `segment`, `hotjar`, `sentry`, `favicon`, `font`, `css`
5. Body parses as valid JSON

---

## 2. Name-field gate

Present. Only `cands[0]` is inspected:

```python
if any(k in cands[0] for k in ["name","productName","cleanName","title",
                                 "productTitle","cardName"]):
    intercepted.append((url, cands))
```

**Risk:** if the first element of a real product list is a non-product object
(e.g. a pagination envelope or count row), the six-key check fails and the
**entire batch is silently dropped**, even if all remaining items are valid
products.

---

## 3. Wait loop

Breaks on **first intercept**; maximum wait is 12 s (12 × 1 s):

```python
for _ in range(12):
    await page.wait_for_timeout(1000)
    if intercepted:
        break
```

**Risk:** any XHR responses that arrive after the first intercept are never
awaited. In practice `intercepted` will usually hold only one batch regardless
of how many the SPA actually fires.

---

## 4. Outer exception handler

Completely silent:

```python
except Exception:
    return ([], None)
```

Any Playwright crash or unexpected error is swallowed with no log entry.
The caller receives `([], None)` with no indication of what failed.

---

## 5. Result extraction

All batches in `intercepted` are processed:

```python
for (intercept_url, item_list) in intercepted:
    for item in item_list:
        ...
```

Iteration does **not** stop after the first non-empty batch; deduplication is
handled by `seen_names`. However, because the wait loop (§3) exits on first
intercept, `intercepted` typically contains only one batch anyway.

---

## 6. Quantity guard (bonus finding)

```python
qty = item.get("quantity") or item.get("qty") or item.get("stock") or 1
```

When none of the three keys are present, `qty` defaults to `1`. This means
items with no stock field pass the `<= 0` check and are returned as available.

---

## Summary table

| # | Location | Issue | Severity |
|---|----------|-------|----------|
| 1 | `on_resp` name-field gate | Only `cands[0]` checked; leading non-product object drops whole batch | Medium |
| 2 | Wait loop | Breaks on first intercept; later batches never collected | Medium |
| 3 | Outer `except` | Silent; zero observability on Playwright failure | High |
| 4 | Quantity fallback | Missing qty field treated as qty=1; zero-stock items may leak through | Low |
