---
name: Verified scraper recipe contracts
description: Runtime matching and precedence rules for verified per-university discovery recipes.
---

Sitemap `allow_url_patterns` and supplement filters must match absolute URLs in the real discovery path, even when some isolated helpers or tests operate on parsed paths. Write patterns that intentionally support the runtime representation, and test them against representative absolute sitemap URLs.

**Why:** A path-anchored recipe correctly matched isolated path-only checks but rejected every real sitemap candidate because production passed full URLs into the same regex.

**How to apply:** For sitemap-backed recipes, run the real discovery function and assert accepted/rejected absolute URLs plus the expected catalogue count.

Critical fields in a verified per-university YAML recipe may be declared as locked config paths. Restore those values after generated and operator configuration merges; leave unrelated fields operator-configurable.

**Why:** Stale generated and admin discovery rules narrowed a verified main catalogue to an obsolete online-only subset.

**How to apply:** Lock only transport/discovery fields whose live evidence has been verified, and regression-test stale generated/admin overrides against the merged runtime config.