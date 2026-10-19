---
name: Northumbria catalogue scope
description: Official catalogue route variants and the limits of sitemap-based completeness.
---

Northumbria's current catalogue includes both single-segment course routes and
routes beneath `/courses/undergraduate/` and `/courses/postgraduate/`. Some
London offerings redirect to the official `london.northumbria.ac.uk` host.

**Why:** A repair that admitted `/courses/<slug>/` recovered the reported
Accounting and Finance MSc, but still excluded current, full-time courses
linked by the official catalogue. Direct requests confirmed those excluded
routes reached real course pages, not navigation.

**How to apply:** Check the live catalogue's offering cards, including grouped
duration/start-date variants, before defining the discovery denominator.
Verify redirected course identity and international eligibility rather than
broadly allowing every descendant URL. Do not equate sitemap entries with
current courses: the sitemap includes child content and courses absent from
the current finder.
