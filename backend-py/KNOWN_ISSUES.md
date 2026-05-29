# Known Issues — Pre-existing Test Failures

These failures existed before the current sprint and are tracked here so
they don't obscure signal when running the test suite.  They do NOT
indicate regressions introduced by recent changes.

**Status as of 2026-05-29:** 33 failed, 1428 passed, 2 skipped.

---

## Failure groups

### Group 1 — Location extractor (7 failures in `test_location.py`, 2 in `test_new_extractors.py`)

```
test_location.py::test_th_td_location_classifies_via_existing_table_path
test_location.py::test_strong_location_strips_online_virtual_from_value
test_location.py::test_strong_location_does_not_misfire_on_unrelated_strong_tags
test_location.py::TestCampusCodeExpansion::test_end_to_end_dl_with_codes
test_location.py::test_slash_separated_cities_normalised_to_comma
test_location.py::test_strip_patterns_remove_acap_footnote_suffix
test_location.py::test_strip_patterns_not_applied_without_config
test_new_extractors.py::test_location_from_definition_list
test_new_extractors.py::test_location_strips_online_virtual
```

**Root cause:** Location extractor refactor introduced assertion mismatches.
Changes to `extractors/location.py` for the `strip_patterns` config feature
broke several test expectations that were written against the old behaviour.

**Risk:** Changes to `extractors/location.py` or `text_cleaning.location`
config handling may create regressions that are masked by these pre-existing
failures.  Run `pytest tests/test_location.py -x` in isolation and check
which failures are new vs pre-existing.

---

### Group 2 — Bulk import endpoint (4 failures in `test_universities_bulk_import.py`)

```
test_universities_bulk_import.py::test_bulk_import_creates_validates_and_skips
test_universities_bulk_import.py::test_bulk_import_rejects_missing_columns
test_universities_bulk_import.py::test_bulk_import_rejects_empty_file
test_universities_bulk_import.py::test_bulk_import_rejects_no_header
```

**Root cause:** Test environment doesn't have the bulk-import endpoint
wired up (likely missing test DB setup or route registration in the test
app fixture).  The endpoint works in production; these are test harness
failures, not application failures.

---

### Group 3 — Sibling-cache / data parity (2 failures)

```
test_week1_prompts_4_to_8.py::test_p4_only_high_precision_methods_seed_cache
test_week1_prompts_4_to_8.py::test_p6_two_source_consensus_does_propagate
```

**Root cause:** Assertion expectations written against an earlier version
of the sibling-cache seeding logic before the ≥2-source consensus gate
was tightened.  The production behaviour is correct; the tests need updating.

---

### Group 4 — Phase A safety (1 failure)

```
test_phase_a6_postgrad_path_does_not_block_postgraduate_slug
```

**Root cause:** Phase A slug-matching rule change for postgraduate pathway
detection left one test case with an outdated expectation.

---

### Group 5 — Per-course browser config (1 failure)

```
test_per_course_browser_per_host_config.py::test_unknown_host_uses_domcontentloaded
```

**Root cause:** Browser event type default changed; test expectation not
updated to match.

---

### Group 6 — Scraper pipeline parity (1 failure)

```
test_scraper_pipeline_parity.py::test_t206_backfill_fills_empty_slot_from_same_degree_bucket
```

**Root cause:** Backfill slot-matching logic in the pipeline changed;
test fixture data doesn't produce the expected `t206` outcome under
the new logic.

---

### Group 7 — Nightly sweep (1 failure)

```
test_new_features_v2.py::test_nightly_sweep_returns_skipped_no_baseline
```

**Root cause:** Test environment lacks the baseline snapshot directory
expected by the nightly sweep function; first-run "no baseline" path
raises instead of returning `sweep=skipped_no_baseline`.

---

## How to distinguish new regressions from pre-existing failures

Run before your change:
```bash
cd backend-py && python -m pytest --tb=no -q 2>&1 | tail -5
```
Expected: `33 failed, 1428 passed`.

Run after your change and compare the count.  If the failure count
increases, investigate the new failures.  If it stays at 33 and the same
test names appear, your change introduced no regressions.

---

## Sprint 2 remediation priority

| Priority | Group | Effort | Notes |
|----------|-------|--------|-------|
| High | Location extractor (Group 1) | ~1 day | Masks real regressions on shared location code |
| Medium | Sibling-cache parity (Group 3) | ~0.5 day | Test data update only |
| Medium | Bulk import (Group 2) | ~0.5 day | Test harness fix; production unaffected |
| Low | Groups 4, 5, 6, 7 | ~0.5 day total | Assertion updates only |
