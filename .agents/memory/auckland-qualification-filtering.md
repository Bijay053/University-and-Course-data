---
name: Auckland qualification filtering
description: How to avoid treating University of Auckland subject pages as qualifications.
---

University of Auckland’s study-option listing mixes qualification records and
subject/major pages under the same URL path. Use its discovery metadata:
records beginning `Subject name:` are not qualification pages, while
`Programme name:` records remain candidates.

**Why:** URL-only filtering admitted hundreds of subject pages. They triggered
browser and AI enrichment, produced incomplete staged rows, and substantially
inflated runtime and candidate totals.

**How to apply:** Filter the anchored discovery label before extraction. Do not
block the whole shared URL path or unanchored occurrences of “Subject name,”
because real programme metadata can mention subjects later in its text.