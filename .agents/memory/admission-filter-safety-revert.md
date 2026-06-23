---
name: Admission filter safety revert
description: strip_non_admission_content can reduce visible text to zero on CMS sites that wrap all body sections in elements matching _NON_ADMISSION_CLASS_FRAGS; safety-revert heuristic prevents this.
---

## Rule
`filter_admission_html()` in `admission_text_filter.py` now reverts to the original HTML when the filter reduces visible text to < 15% of the original (absolute floor: 150 chars), provided the original had > 300 visible chars.

**Why:** Teesside University's ColdFusion CMS wraps all course page body sections in DOM elements whose CSS class names happen to match `_NON_ADMISSION_CLASS_FRAGS` (e.g., `course-structure`). The filter removed all body content, leaving an empty `<main>`, which `_extract_content_html` + `html_to_text` turned into `text_len=0`. Gemini then skipped with `skip_reason='empty_page'`.

**How to apply:**
- The fix is in `admission_text_filter.py` and fires automatically — no YAML needed for the general case.
- For universities where the filter is confirmed to over-strip, add `strip_non_admission_content: false` to their YAML as belt-and-suspenders (Teesside has this).
- The safety-revert log line is `[ADM-FILTER] safety-revert for {url}: filter reduced visible text {N} → {M} chars ({pct}%) — reverting to original HTML` at WARNING level.
- Threshold: `_orig_visible > 300 AND _filt_visible < max(150, _orig_visible * 0.15)`.
