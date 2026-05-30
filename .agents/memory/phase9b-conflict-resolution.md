---
name: Phase 9B Conflict Resolution
description: Deep conflict resolution architecture — T001-T004 components and their integration order
---

## Repair Loop Order (mandatory)

When `repair_course_conflicts()` processes a conflict field:
1. **T002 normalization_equivalence** — cheapest, no I/O; re-applies improved normalizers to existing evidence. If all sources now agree → resolved.
2. **Source priority (drop_low_authority)** — existing logic, drops AI/pattern sources, checks if high-auth sources agree.
3. **T001 source_revalidation** — async HTTP re-fetch fallback; only fires if both above fail.

**Why:** This order minimises latency (cheap checks first) and avoids unnecessary HTTP calls.

## Resolution Confidence Formula

| action_taken | confidence |
|---|---|
| normalization_equivalence | 85 + (n_resolved_by × 3), max 100 |
| drop_low_authority, ≥2 high-auth | 95 |
| drop_low_authority, 1 high-auth | 80 |
| source_revalidation | 88 |
| unresolved | 0 |

## Field Normalizer Canonical Forms

- **duration** → whole months as string ("24", "18") — handles years/months/weeks/semesters/written-out
- **fee** → float string nearest 100 ("45000.0") — handles k-suffix, currency prefix, ranges
- **ielts/score** → 1-decimal float string ("6.5") — extracts first numeric token
- **intake_months** → month number string ("2"–"12") — handles month names, trimesters, sessions, plain ints

## Schema (migration 028)

`conflict_repair_log` got 3 new columns in Phase 9B:
- `resolved_by JSONB` — list of source types that agreed
- `resolution_confidence INTEGER` — 0–100
- `resolution_method VARCHAR(60)` — "drop_low_authority" | "normalization_equivalence" | "source_revalidation" | "unresolved"

Back-filled existing rows: `resolution_method = action_taken` where resolved, else 'unresolved'.

## T004 KPI API Shape

`GET /api/verification/summary` → `repair_stats`:
```json
{
  "conflict_resolution_rate": 92,   // int 0-100 or null if no repairs run
  "resolution_breakdown": [
    {"method": "drop_low_authority", "count": 47, "avg_confidence": 87},
    {"method": "normalization_equivalence", "count": 38, "avg_confidence": 91}
  ]
}
```

## Pre-Existing Test Failures (NOT caused by Phase 9B)

- `test_phase7_quality_actions.py::TestGetAvgCompleteness::test_returns_float_from_scalar`
- `test_stage_evidence_and_review.py::test_stage_course_persists_completeness_and_evidence` — single-source HTML gives 65% confidence → auto_publish_status="review" not "ready"
