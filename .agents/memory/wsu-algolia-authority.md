---
name: WSU international catalogue authority
description: Source precedence and exception handling for Western Sydney University course data.
---

Western Sydney University’s public course detail HTML defaults to domestic CSP content. The international-filtered Algolia catalogue is authoritative for international fees and full-time duration; values from it must retain API provenance rather than citing the domestic page.

**Why:** Re-scraping detail pages produced zero international fees and allowed fallback AI to infer implausible postgraduate durations even though the international catalogue already published both fields.

**How to apply:** Preserve configured Algolia metadata through discovery without URL caching, and treat its international-filtered links as the authoritative course set rather than reapplying stale BFS/admin URL deny rules. Merge metadata deterministically after page extraction. Use WSU’s official international English baseline only as a fill-only source, with title-matched nursing, allied-health, medicine, teaching, social-work, and psychology exceptions; do not treat valid bachelor titles containing “Pathway to Teaching” as preparatory programs.