---
name: Browser rescue override parity
description: Safety rule for unresolved-course retries when browser recovery was disabled by scraper configuration.
---

When an operator enables browser rescue for unresolved courses, clear both independent browser-suppression controls together. If either remains active, the retry is not a rescue and must not be presented as one.

**Why:** A real continuation chain announced browser rescue but changed only one control. The other still suppressed every per-course browser attempt, so two long retries repeated the same failures while the interface continued asking the operator to continue.

**How to apply:** Detect either suppression reason in structured job logs, persist explicit overrides for both controls, and hide ordinary continuation while either reason is present. Keep numeric UI labels separate from error counters unless they come from the reconstructed unresolved URL set.