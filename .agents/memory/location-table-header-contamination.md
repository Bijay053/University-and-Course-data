---
name: Location table-header contamination
description: Safety rule for location tables whose adjacent cells are metadata headers rather than campus values.
---

Treat a table row beginning with “Location” as column metadata when it is inside a table header or consists entirely of header cells. Adjacent labels such as “Domestic”, “International”, or “Teaching period” are not campus values. Period-based availability tables may still collect physical campuses from their data rows, while Online rows remain non-physical. Removing virtual-delivery words must also remove and reject conjunction-only residue such as “and” or “or”.

**Why:** Course-unit tables can contain `Location | Teaching period` or `Location | Domestic | International`. Accepting the second header as a campus can make an Online-only course appear to have a physical location, which then incorrectly changes its mode to On Campus and bypasses the global Online-only rejection. Phrases such as “Distance and online” can otherwise normalize to the literal location “and”.

**How to apply:** Validate the DOM role and geographic content before treating a location candidate as physical. Field labels, audience labels, schedule labels, conjunction-only remnants, and nearby prose must fail closed; only verified physical data rows may justify overriding an Online signal.