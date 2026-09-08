---
name: UQ start-semester intake authority
description: The authoritative source and fail-closed rule for University of Queensland program intake extraction.
---

For `study.uq.edu.au` program pages, derive intake only from the selected
program/year/audience Drupal settings value `uqGtmInitial.start_semester`.
Preserve the explicit month and day published there. If that fact is absent or
unparseable, leave intake unresolved rather than scanning the full page.

**Why:** UQ program pages contain many unrelated application, navigation,
scholarship, event, and academic-calendar dates. Generic page-wide date
extraction produced plausible-looking but catalogue-wide false intake months.

**How to apply:** Treat the selected-program start-semester fact as a
host-specific authority ahead of generic extraction. Re-extractions must force
the intake field so previously populated regex values can be replaced.