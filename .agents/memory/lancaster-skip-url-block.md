---
name: Lancaster skip_url_block fix
description: course_detail_url_patterns bypass in orchestrator pre-extraction gate does NOT protect against the second is_blocked_page() call inside stage_course.py — causing 0 staged for universities whose URL paths contain a global block substring.
---

## The bug

`stage_course.py` calls `is_blocked_page()` independently of the orchestrator's pre-extraction gate. The pre-extraction gate at orchestrator line ~2154 respects `course_detail_url_patterns` as a bypass, but `stage_course` makes a second call that has no such bypass — so any university whose course URLs contain a global block substring (e.g. Lancaster `/study/postgraduate/` or `/study/undergraduate`) gets `category_landing_page_url_block` *after* extraction, staging 0 courses despite correct extraction.

Symptom: FIELD TRACE logs show correct extracted data (fee, IELTS, etc.), immediately followed by `blocked_page rejected 'Course Name': category_landing_page_url_block (url)`.

## The fix

Three-part change:

1. **`stage_course.py`**: Add `skip_url_block: bool = False` parameter. Wrap the `is_blocked_page()` call: `if source_url and not skip_url_block:`.

2. **`orchestrator.py`**: Move `_gate_detail_pats` initialization **outside** the `if is_blocked_page is not None and links:` block so the variable is always in scope (including at the stage_course call site deep in the extraction loop, ~1500 lines later).

3. **`orchestrator.py`** at the `stage_course()` call: compute `_skip_url_block = bool(_gate_detail_pats and any(p.search(_cur_url) for p in _gate_detail_pats))` and pass `skip_url_block=_skip_url_block`.

**Why:** `course_detail_url_patterns` is the operator's explicit allow-list. Once a URL has passed that allow-list in the link gate, running the global block-list again in stage_course contradicts the operator's intent and falsely rejects valid course pages.

**How to apply:** Any future university where `course_detail_url_patterns` is needed but staging is still 0 (with FIELD TRACE showing good data) — check `stage_course.py` for a second `is_blocked_page()` call. The `skip_url_block` flag is the correct fix; do not widen the global block-list instead.

## Lancaster context

Lancaster (uni_id=1901) has 538 courses (371 UG + 167 PG) discovered via SSR Vue prop (`:courses-data` JSON blob). Course URLs are `/study/undergraduate/courses/[slug]/2026/` and `/study/postgraduate/postgraduate-courses/[slug]/2026/`. Both contain substrings that match global block rules (`/study/undergraduate` line 1093, `/study/postgraduate/` line 1113 in guards.py).

Result after fix: **533/538 staged** (was 0/538 in 3 prior attempts). 5 skipped = data_quality_failure (legitimate — low completeness). Cost $0.50 Gemini.
