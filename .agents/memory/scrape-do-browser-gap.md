---
name: scrape.do browser-fallback gap
description: The [BROWSER↑] escalation path in single_course.py bypasses http_fetcher.py's scrape.do chain — must add a separate guard after browser retries exhaust.
---

## Rule
The `scrape_do_fallback: true` YAML flag was silently doing nothing for courses that triggered `[BROWSER↑]`. The http_fetcher.py scrape.do path (lines ~399-405) is only reachable when `fetch_html_with_browser()` is called as part of http_fetcher's own chain. When single_course.py's `[BROWSER↑]` block calls `_bp.fetch_html()` directly and that also fails, control returns to line 1428 which immediately returns `fetch_failed` — scrape.do is never tried.

**Why:** Two independent fetch escalation paths exist: (1) http_fetcher.py chain: HTTP → cffi → Wayback → scrape.do; (2) single_course.py `[BROWSER↑]` path: HTTP blocked → browser pool → [retries] → (gap here) → fetch_failed. Path 2 bypasses path 1 entirely.

**How to apply:** After the `[BROWSER↑]` retry loop, add:
```python
if not html:
    from app.services.scraper import http_fetcher as _http_fetcher_mod
    from app.services.scraper.http_fetcher import fetch_html_scrape_do
    if _http_fetcher_mod._scrape_do_enabled:
        html = await fetch_html_scrape_do(url)
```
Import the MODULE not the variable — `from module import bool_var` captures the value at import time (False), not the live value after `set_scrape_do_fallback(True)` ran. Accessing `_http_fetcher_mod._scrape_do_enabled` reads the live module attribute.

**Symptom:** Courses 299-433 show `fetch_failed` for Cloudflare-heavy universities (WLV, UTAS) even when `scrape_do_fallback: true` is set. Log shows `[BROWSER↑]` for every blocked course but zero `[SCRAPE.DO↑]` lines.

**Fix location:** `backend-py/app/services/scraper/pipelines/single_course.py` — after the `[BROWSER↑]` except block, before `if not html: return fetch_failed`.
