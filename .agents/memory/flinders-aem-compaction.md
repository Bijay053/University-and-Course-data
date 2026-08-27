---
name: Flinders AEM page compaction
description: Why Flinders course HTML should be reduced before generic extraction.
---

Flinders course pages are unusually large AEM documents dominated by navigation,
related-course, and footer markup. Keep the document metadata, course title shell,
and authoritative fast-facts component, and discard the surrounding chrome before
running the generic extractor suite.

**Why:** Concurrent HTTP requests were fast, but generic extractors repeatedly
parsed each roughly 750 KB document on one event loop. That serialized the batch
and made the scrape look network-bound. Compaction retained all verified core
fields while reducing local per-course extraction by more than an order of
magnitude.

**How to apply:** Preserve fail-open behavior if the expected title or fast-facts
component is absent. Re-verify title, fee, IELTS, duration, intakes, location, and
CRICOS whenever Flinders changes its AEM template.