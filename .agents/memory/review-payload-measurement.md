---
name: Review payload measurement
description: Measure all requests involved in opening Review, not only the main list endpoint.
---
Measure the entire Review-opening request sequence when validating payload savings.

**Why:** A pending-count fetch and the separate history-detail response can still eagerly download evidence even after the main Review list switches to summaries. An isolated endpoint benchmark therefore overstates the user-visible savings.

**How to apply:** Include background/count reads and history navigation in browser network assertions, alongside database-backed endpoint byte measurements. Label mocked browser timings separately from real HTTP/database measurements.

Treat grouping metadata as part of the public summary contract, not disposable source evidence.

**Why:** Legacy campus rows use provenance metadata to identify one logical course. Removing it changes selection, campus fee labels, and edit behavior even when all scalar course fields survive.

**How to apply:** Exercise summary-shaped sibling rows through grouping, selection, and Edit on both Review and Raw Data. Grouped display rows may be clones and continuation rows may belong to an earlier job; distinguish displayed Review identity from the underlying row identity.