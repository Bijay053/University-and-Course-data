---
name: QUT static extraction and delivery gates
description: Durable transport and course-eligibility rules for Queensland University of Technology scraping.
---

Use QUT’s large static course response as the extraction source and avoid per-course browser rescue. Treat a bare “online” keyword as incidental unless the page provides strong course-owned online-only evidence. Canonical `/courses/<slug>` URLs must not be rejected solely because one title source omits a degree qualifier.

**Why:** QUT’s browser render is Cloudflare-blocked and returns no usable document, while repeated browser attempts exhaust the shared course deadline. QUT pages also contain site-wide online copy and can expose shortened title variants, creating false policy rejections before authoritative domestic/online eligibility checks run.

**How to apply:** Keep QUT on static extraction, require explicit strong evidence for online-only classification, and rely on the canonical course URL plus downstream audience/fee checks instead of the generic degree-title heuristic.