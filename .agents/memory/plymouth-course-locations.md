---
name: Plymouth course locations
description: Location authority on Plymouth course pages with JavaScript-populated application dropdowns.
---

The initial application form's `--Select Location--` option is an unhydrated prompt, not a course location. Its course-owned application offerings can contain physical campus values even when the visible dropdown is empty. Do not assume all Plymouth courses are in Plymouth: partner and satellite offerings may differ, and an empty offering set is not evidence for the university's home city.

**Why:** The prompt was staged verbatim for many courses; rejecting it alone would leave recoverable locations blank. A live page exposed published, active course offerings inside an attribute of a descendant of the course-version widget, not on the course-version element itself.

**How to apply:** Prefer physical locations from published, in-use offerings owned by the current course; reject placeholder and virtual-only values. If offerings are absent or untrusted, preserve an unknown location rather than inventing a default campus. When testing a source-page selector, verify it against real HTML as well as a representative fixture.