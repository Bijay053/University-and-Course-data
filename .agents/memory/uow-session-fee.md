---
name: UOW session-fee table
description: How University of Wollongong’s course page fee table distinguishes per-session tuition from the full-course total.
---

# UOW session fee vs course fee

When a UOW course page has both `Session fee` and `Course fee` columns, use
the session fee as the scraped tuition value and preserve its term as
`Session`. The course fee is the indicative total across the full programme,
not an annual figure.

**Why:** a flattened text extractor sees both currency values in the same
short context and tends to choose the larger total, then mislabels it as
Annual. This substantially overstates the periodic fee.

**How to apply:** keep the extraction host-gated and require both table headers
before using this rule. Do not infer an annual amount by relabelling the
full-course total; surface the source’s actual session cadence.