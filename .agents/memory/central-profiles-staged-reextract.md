---
name: Central profiles in staged re-extraction
description: Durable rules for using and invalidating central English profiles during Course Checking fixes.
---

Review → Fix must prefetch the university's configured central pages and pass that payload into per-course extraction, just as a normal scrape does. Any semantic change to central English parsing must also increment the English cache schema version.

**Why:** A Murdoch course-code parser worked in fresh live-page tests, but Course Checking fixes kept the generic level defaults because re-extraction omitted central data and production accepted an older parsed cache as current.

**How to apply:** When changing central-page parsing or staged re-extraction, verify the recovery call receives `central_data`, verify a previously populated row can be overridden, and use an old-version cache fixture to prove automatic reparsing.