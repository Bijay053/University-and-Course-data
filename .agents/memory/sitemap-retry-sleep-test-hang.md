---
name: Sitemap retry sleeps hang mocked tests
description: Real asyncio.sleep retry backoffs in sitemap._fetch_text make fully-mocked discovery/sitemap tests appear hung (~20s per empty probe candidate).
---

# Sitemap retry sleeps hang mocked tests

**Rule**: Any test that can reach `sitemap._fetch_text` with empty/mocked
responses must zero `sitemap._RETRY_DELAYS` (module-level, monkeypatchable)
or stub `discover_from_sitemap` entirely.

**Why:** `_fetch_text` retries empty responses after real `asyncio.sleep(5)`
and `asyncio.sleep(15)` backoffs. A mocked `fetch_html` returning "" is
instant-empty → both retries fire → ~20s of pure sleeping per probe
candidate. With 4 sitemap index paths + robots.txt that's ~100s per test —
looks like a hang under any reasonable timeout. Also: tests that mock only
`discovery.fetch_html` still hit the REAL sitemap-module `fetch_html`
(separate import) when BFS yields <5 candidates and the sitemap fallback
fires — real network + sleeps.

**How to apply:**
- `test_sitemap_discovery.py` has an autouse `_zero_retry_delays` fixture
  patching `sitemap_mod._RETRY_DELAYS = (0.0, 0.0)`.
- Discovery-level tests should stub `sm.discover_from_sitemap` (the
  convention throughout `test_discovery_regression.py`).
- If a "hanging" test file collects instantly (`--collect-only` fast) but
  produces no output, suspect real sleeps/network first, not import stall.
