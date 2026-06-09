---
name: UWL research-degree page template
description: /course/research/ pages have no SSR blob but a working nationality select; PhD IELTS uses "no element under" not "no band below".
---

## UWL research-degree page template differences

**URL pattern**: `https://www.uwl.ac.uk/course/research/<slug>`  
(vs taught courses at `/course/undergraduate/<slug>` and `/course/postgraduate/<slug>`)

### Fee extraction

Research degree pages **do NOT have the Angular SSR JSON blob** (`field_p_cv_int_main_fee` is absent in all tested pages: mathematics, engineering, business).

They **DO have a nationality pricing `<select>`** element, but it is a generic unnamed select (not `id="nationality_pricing_input_mobile"`). The fallback loop in `_from_uwl_nationality_select` finds it via `_UWL_FEE_OPT_RE` and correctly reads the "– International" option.

**Verified international fees from select (2026-06-09):**
- PhD Mathematics: £14,000
- PhD Engineering: £42,194

The old scrape showing £6,000 for PhD Mathematics was from a pre-blob-fix scrape where the generic scanner extracted the UK home rate.

### Safety net (added 2026-06-09)

If a UWL `/course/research/` page has neither blob nor a select that matches `_UWL_FEE_OPT_RE` (edge case for newly-added programmes), `_from_uwl_nationality_select` now returns `_UWL_DOMESTIC_ONLY` instead of falling through to the generic scanner. This prevents the UK self-funded PhD rate (typically £4,500–£6,000/yr) from being extracted as the international fee.

### IELTS phrasing

Research-degree pages use a **different IELTS phrasing** than taught courses:

- Taught (UG/PG): `"IELTS 6.5 or above with a minimum of 5.5 for each of the individual components"`  
  → Pattern 4.6 (split overall+band) or Pattern 5 + `_try_floor`

- Research (PhD): `"An IELTS score of 6.5 (with no element under 6.0)"`  
  → Pattern 1b: requires `"no element under"` (word "element" not "band/component/score")

**Fix applied**: added `"element"` to:
1. `_PER_BAND_FLOOR_RE` regex (used by `_try_floor`)
2. Pattern 1b alternation: `(?:band|component|score|element)`

Result: PhD Mathematics now extracts `ielts_overall=6.5`, `ielts_listening=6.0`, `ielts_reading=6.0`.

**Why:** UK universities (Torrens, ACAP, UWL) all have slightly different IELTS wordings. "element" = "component" = "band" = "score" in this context — they all refer to per-skill subscores.

**How to apply:** If a university uses "no element under X" or similar in IELTS requirements, verify `_PER_BAND_FLOOR_RE` and Pattern 1b cover the keyword. Check by running `_ielts(text)` in a test.
