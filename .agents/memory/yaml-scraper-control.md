---
name: 95% YAML scraper control
description: New university onboarding requires only YAML edits — 5 new ExtractionConfig fields replace 5 hardcoded per-host Python lists.
---

## Rule
When onboarding a new university, edit ONLY its `.yaml` file — no Python source changes needed for the most common behaviours.

**Why:** Previously every CF-protected UK uni required code changes in 2+ Python files. This created a high barrier for operators and risked breaking shared logic.

## New YAML fields → replaces Python list

| YAML field | Replaces hardcoded list | File |
|---|---|---|
| `extraction.retry_on_cloudflare: true` | `_BROWSER_RETRY_HOSTS` | `per_course_browser.py` |
| `extraction.force_browser: true` | `_FORCE_BROWSER_HOSTS` | `per_course_browser.py` |
| `extraction.needs_international_toggle: true` | `_INTERNATIONAL_TOGGLE_HOSTS` | `per_course_browser.py` |
| `extraction.study_mode.suppress_nav_rule: true` | `_STUDY_MODE_RULE_SUPPRESSED_HOSTS` | `study_mode.py` |
| `extraction.english.suppress_pte: true` | `_NO_PTE_HOSTS` | `english_test.py` |

## Pre-existing YAML fields (also no code needed)

| YAML field | Controls |
|---|---|
| `extraction.skip_per_course_browser: true` | `_SKIP_BROWSER_HOSTS` |
| `extraction.max_parallel_fetch: N` | `_HOST_CONCURRENCY_CAPS` |
| `extraction.browser_wait_strategy: networkidle` | `_NETWORKIDLE_HOSTS` |
| `extraction.browser_dcl_settle_ms: N` | `_DCL_SETTLE_MS_OVERRIDES` |

## Pattern: YAML-first, hardcoded-list fallback
Each module checks the contextvar YAML config first, then falls through to the hardcoded list. This means old unis with hardcoded entries still work unchanged; new unis get YAML-only configuration.

## Migrated out of code to YAML (2026-06-04)
- UEL (`uel.yaml`): `retry_on_cloudflare: true`, `study_mode.suppress_nav_rule: true`
- WLV (`wlv_1761.yaml`): `retry_on_cloudflare: true`, `study_mode.suppress_nav_rule: true`
- Canterbury still in `_STUDY_MODE_RULE_SUPPRESSED_HOSTS` (no YAML file migration done)
