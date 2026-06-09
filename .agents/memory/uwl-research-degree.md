---
name: UWL research-degree page template
description: /course/research/ pages share a GENERIC blob (int=14000 all courses); actual per-course fee is JS-only; "no element under" is PhD IELTS phrasing.
---

## UWL research-degree page template differences

**URL pattern**: `https://www.uwl.ac.uk/course/research/<slug>`  
(vs taught courses at `/course/undergraduate/<slug>` and `/course/postgraduate/<slug>`)

### Fee extraction — critical finding

Research degree pages **DO have the Angular SSR JSON blob** but it is a **shared generic placeholder**:
- `field_p_cv_int_main_fee` → `14000` (same for ALL research courses)
- `field_p_cv_uk_eu_main_fee` → `4400` (same for ALL research courses)

This is NOT a per-course fee. Actual per-course fees differ:
- PhD Media: £16,000 (from screenshot / JS-rendered select)
- PhD Mathematics: different (not £14,000)
- PhD Law: different

The actual per-course fee is ONLY in the **JS-rendered `<select>`** which is **empty in static HTML** (render=False mode).

**Fix (2026-06-09)**: Added a URL-path guard at the TOP of `_from_uwl_nationality_select`, BEFORE the blob check. For any URL containing `/course/research/`, the function immediately returns `_UWL_DOMESTIC_ONLY` — blob is never read, generic select is never searched. These courses are skipped by the `no_international_fee` gate. Operators must fill in research degree fees manually.

**Why the guard must be BEFORE the blob check**: if placed after, the generic £14,000 blob value is returned for every PhD research course, giving the same wrong fee to all courses.

### Select options (static HTML)

All research pages: select options are **empty** in static HTML — JS-populated only. Confirmed for media, law, mathematics, aviation.

### IELTS phrasing

Research-degree pages use: `"An IELTS score of 6.5 (with no element under 6.0)"`

Taught-course phrasing: `"IELTS 6.5 or above with a minimum of 5.5 for each of the individual components"`

**Fix**: added `"element"` to `_PER_BAND_FLOOR_RE` and Pattern 1b alternation so "no element under X.0" is captured as the per-band floor.

**How to apply:** If a UWL research course scrape shows fees (non-empty), something is bypassing the URL guard — check the URL path extracted by urlparse. If IELTS bands are missing for PhD courses, check that "element" is in the Pattern 1b and `_PER_BAND_FLOOR_RE` alternations.
