---
name: Phase 9 Verification Engine
description: Cross-source confidence scoring for scraped fields; source weights, conflict logic, auto-publish gate integration
---

## Source weights
html=30, pdf=30, api=30, pattern=5, ai=5

## Source classification (classify_source_type)
- gemini:*, ai_fallback, ai_primary → "ai"
- uni_pdf:*, pdf:*, cricos_match → "pdf"
- searchstax:*, json_api, solr, *api* → "api"
- sibling_cache:*, approved_row, pattern → "pattern"
- everything else → "html"

## Confidence formula
- score = sum(weights) for sources that agree with consensus value
- Conflict (any source disagrees): score = score // 2, capped at 35, status = "conflict"
- Status: verified ≥ 85, likely_correct 60–84, needs_review < 60 (no conflict)

## Auto-publish gate
- avg_verification_confidence IS NULL → does NOT block (engine hasn't run yet)
- avg_verification_confidence < 85 → blocks with "Phase 9" reason
- Gate is inside should_auto_publish() in auto_publish.py

## Key files
- verification_engine.py — classify_source_type, compute_field_confidence, run_field_verification
- field_verification_results — table; UNIQUE (scraped_course_id, field_name)
- scraped_courses.avg_verification_confidence — FLOAT column (migration 025)
- verification.py router — /api/verification/course/{sc_id}, /university/{id}/summary, /dashboard
- university-detail.tsx — "Verification Intelligence" sky-blue card, fetched on mount, auto-shows if total_fields_verified > 0

**Why:** Agreement-based confidence is more reliable than presence-based; a field from three independent sources (html+pdf+api all agree) should auto-publish without manual review.
