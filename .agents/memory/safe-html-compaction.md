---
name: Safe cross-site HTML compaction
description: Safety requirements for reducing large course-page DOMs across universities.
---

Cross-site HTML compaction must be disabled by default and enabled per university
only after raw-versus-compacted full-pipeline payload parity and latency have both
been measured. Collapse validated chrome structure while preserving its visible
text and document order.

**Why:** Removing navigation and footer text changed duplicated IELTS and intake
candidates on real Lancaster and Swinburne pages. Flattened-text fingerprints also
cannot protect JSON-LD, tables, form-labelled values, CSS rules, or other
relationship-dependent extraction.

**How to apply:** Never flatten a candidate that is, or contains, scripts, tables,
templates, forms, or controls. Keep absent-config and API defaults off, fail open
on unsafe selectors or signal drift, and re-run full payload parity whenever a
site template changes.