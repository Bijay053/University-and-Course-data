---
name: UWL research-degree page template
description: /course/research/ pages have 4 fee options in SSR blob; max = full-time £16,000; "no element under" is PhD IELTS phrasing.
---

## UWL research-degree fee blob structure

**URL pattern**: `https://www.uwl.ac.uk/course/research/<slug>`

### Fee extraction — CRITICAL: 4 fee options in SSR blob

All research pages embed **4 distinct fee entries** in `field_p_cv_int_main_fee` across the SSR JSON blob (UWL embeds the blob twice so each option appears twice):

| Fee | Role |
|-----|------|
| 14000 | **Generic CMS placeholder** — FIRST in blob, NOT a real fee |
| 16000 | **Full-time international** ← CORRECT (shown in JS dropdown) |
|  8000 | Part-time per-year rate A |
|  7000 | Part-time per-year rate B |

The standard blob reader takes the FIRST match (14000) → WRONG for all research courses.

**Fix**: the `/course/research/` URL guard collects ALL `field_p_cv_int_main_fee` values and returns the MAXIMUM. Since 16000 > 14000 > 8000 > 7000, the maximum is always the full-time fee. Verified across: law, media, mathematics, engineering, business, criminology, design, aviation.

**UG/PG courses are unaffected** — the guard only fires for `/course/research/` URLs.

### Select options (static HTML)

All research pages: the select is **empty in static HTML** (JS-populated). The blob max-fee approach is the only reliable static-mode extraction for research degrees.

### IELTS phrasing

Research-degree pages use: `"An IELTS score of 6.5 (with no element under 6.0)"`

Taught-course phrasing: `"IELTS 6.5 or above with a minimum of 5.5 for each of the individual components"`

**Fix**: added `"element"` to `_PER_BAND_FLOOR_RE` and Pattern 1b alternation so "no element under X.0" is captured as the per-band floor.

**Why:** The CMS placeholder 14000 always appears FIRST because it's a top-level course-node default in the Drupal content type. The actual study-option entries (16000/8000/7000) are nested in `field_p_cv_study_options` and follow it in the serialized JSON.

**How to apply:** If a UWL research course shows a wrong fee, check that the max-blob logic fires (URL must contain `/course/research/`). If the scraper shows £14,000 for a research course, the `/course/research/` guard is not matching the URL — check urlparse path extraction.
