---
name: Search English-score casing
description: Why live course search must not copy the retired view's case-sensitive English-score predicates.
---

Course-search projections must case-normalize `english_requirements.test_type` and select duplicate scores deterministically (currently the maximum), rather than copying the retired materialized view's exact case-sensitive `LIMIT 1` predicates.

**Why:** Production data predominantly stores test types in lowercase. Exact uppercase predicates made thousands of IELTS, PTE, TOEFL, Cambridge, and Duolingo values disappear from result badges and score filters. Unordered `LIMIT 1` also varied when a course had duplicate requirement rows.

**How to apply:** When changing the search row source or rebuilding its regression fixture, include lowercase test types and duplicate rows. Validate score-populated course counts and score-filter behavior against live-shaped data, not only the historical view SQL text.