---
name: HSU campus and tuition authority
description: Course-owned campus and audience/year boundaries on Health Sciences University pages.
---

Read HSU location from the current course's labelled fact, never the site-wide
campus navigation or footer. Match international tuition to the latest tuition
year block and its own International heading; retain an explicit full-course
term rather than treating every number as annual.

**Why:** The user's scrape joined Bournemouth, London and Reading navigation
labels into every course's location and selected a UK tuition amount as the
international fee. The current Occupational Therapy page places a UK price
beside “Fees for overseas students are still to be confirmed”; that is not
international tuition evidence. Chiropractic publishes different dated
domestic/international blocks and spaced thousands separators.

**How to apply:** Keep authoritative omissions through fallback processing,
leave unknown overseas tuition blank, preserve GBP/year/term together, and
test selected evidence as well as the final numeric value. A global campus
default or a domestic price is not a safe substitute for missing source facts.
