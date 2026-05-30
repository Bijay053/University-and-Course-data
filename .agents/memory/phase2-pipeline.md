---
name: Phase 2 autonomous pipeline
description: How Stage 0 AI extraction rules wire into extract_course() and the key gotchas
---

## The rule
Phase 2 adds a Stage 0 block inside `extract_course()` (single_course.py) that applies Gemini-generated CSS/XPath/regex rules BEFORE all regex heuristics and before any per-course Gemini call.

## Evidence dict key convention
Evidence rows appended in Stage 0 MUST use `field_key`, `candidate_value`, `extraction_method`, `selected`, `decision_status` — NOT `field`, `value`, `method`. Using `field` instead of `field_key` causes a `KeyError` downstream at `single_course.py` line ~2269 (`e["field_key"] == "study_mode"` list comprehension).

## _apply_css attribute sentinel
`attribute="text"` in a rule dict is a sentinel meaning "return inner text". The `_apply_css` function must NOT call `el.get("text")` — that is an HTML attribute lookup that always returns None. Rule: `if attribute and attribute.lower() != "text": use el.get(attribute); else: use el.get_text()`.

## Gemini skip logic
When Stage 0 covers ≥85% of the 13 review fields, `use_ai_fallback` is set to False. This makes Gemini cost $0 for that course. The check is `should_skip_gemini(results, review_fields)` in `ai_extractor_run.py`.

## _ac_ext_rules closure
`_ac_ext_rules` is initialised in `run_scrape()` scope from `auto_config.get("extraction_rules")` and passed to `extract_course()` via the `_extract_only()` closure. It does NOT need to be passed as a parameter through intermediate functions — closure captures it.

**Why:** Keeps the extraction rules cost out of every per-course Gemini call once a site has been probed once.

**How to apply:** Any new entry point that calls `extract_course()` directly (new Celery tasks, scripts) must either pass `extraction_rules` explicitly or set it to None.
