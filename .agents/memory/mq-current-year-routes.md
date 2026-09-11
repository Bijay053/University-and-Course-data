---
name: MQ current-year admissions routes
description: Why Macquarie Funnelback course URLs must be resolved through current unversioned admissions routes.
---

Macquarie Funnelback can keep returning an explicit year-stamped admissions URL after the public unversioned route has advanced to a newer intake year. Treat the unversioned admissions route as the current-course authority before fetching page data.

**Why:** A live full scrape used a 2026 Funnelback URL and therefore retained the older fee, while the same course's unversioned public route had advanced to 2027.

**How to apply:** For MQ admissions URLs under `find-a-course/courses`, remove a single `20xx` path segment before page-data enrichment. Keep explicit handbook-year URLs separate because they serve historical discovery.