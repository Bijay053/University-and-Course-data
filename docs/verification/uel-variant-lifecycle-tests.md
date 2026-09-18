# UEL variant lifecycle — offline verification

Verified: 2026-09-18

## Command

```bash
cd backend-py
PYTHONPATH=. python -m pytest -q \
  tests/test_uel_variants.py \
  tests/test_uel_variant_integration.py \
  tests/test_uel_discovery.py
```

## Result

**PASS — 40 passed in 19.55s** (one unrelated Pydantic deprecation warning from `app/routers/snapshots.py`).

| Task 513 requirement | Automated evidence |
|---|---|
| Bounded, cancellable expansion | Stop, deadline, and account-error paths cancel and drain outstanding fetches; source caps, scaled phase budgets, progress, and prefetched HTML reuse pass. |
| Route ownership | MA/MFA, standard/foundation, and placement routes retain semantic URL identity and bind only their own option rows and requirement panels; missing panels cannot borrow sibling/global values. |
| Staging, approval, retry, resume, and re-extraction | Variant fan-out, targeted retry/checkpoint behavior, raw-snapshot replay, isolated staging/approval identities, same-route deduplication, re-extraction, null clearing, and ambiguous legacy-parent review all pass. |
| Independent IELTS components | Distinct route overall scores and grouped Writing/Speaking versus Listening/Reading minima remain independent. |
| Explicit Home-only eligibility | Home Applicant routes are marked domestic-only and rejected by the global staging gate; international part-time routes do not borrow Home full-time data. |
| Malformed or missing eligibility | Missing application/attendance evidence and incomplete multi-award templates fail closed as unknown/template errors rather than being fabricated as domestic. |
| Catalogue discovery | Static undergraduate/postgraduate catalogue configuration and course-detail URL filtering pass using local fixtures. |

## Safety and scope

No live catalogue scrape or external request was made: UEL transport and catalogue responses were mocked/local. No production data was accessed or approved. The DB lifecycle test created a uniquely named disposable university in the configured development test database, approved only its synthetic staged rows, and removed the university in cleanup.

No additional tests were needed; the existing targeted suite directly covers the requested offline lifecycle requirements. Full-catalogue runtime counts and elapsed time remain outside this offline verification and still require the separately authorized post-release run described by Task 513.