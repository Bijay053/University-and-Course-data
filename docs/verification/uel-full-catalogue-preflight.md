# UEL full-catalogue runtime verification — blocked preflight

Original preflight on 2026-09-18. The blockers below were subsequently resolved
by user-authorized release and an isolated full run. See
[final results](uel-full-catalogue-result.md): verification completed with
acceptance failures. The following preserves the original preflight evidence.

## Production observations (read-only)

- External AWS production repository HEAD:
  `366e563450636310583b103937ec322f50178989` (`Fix UEL doctorate duration and level`).
- `backend-py/app/services/scraper/extractors/uel_variants.py` is absent on that release.
- Production orchestrator SHA-256:
  `ffd060b7dce8aa52088c82ed6f01ebdb621eae0bae0af7cdc5fc54ab2fe650d7`.
- Both API and Celery services reported active.
- UEL university ID: `69`.
- Most recent existing job: `job_ce7e15ada879`, completed, created
  `2026-09-18 00:23:27.008947+00:00`, progress `current=276`.
- Existing review: **253 pending rows / 253 distinct canonical URLs**, all
  with `auto_publish_status=review`, belonging to that completed job.

The old job's progress value is not evidence of post-change source-page or
variant-route coverage. No source/route counts, expansion timing, or fresh
source-panel IELTS comparison can be claimed from this preflight.

## Why no scrape was launched

1. The required variant changes are not released. A run on the deployed code
   cannot verify them. Deployment was not part of the verification authorization.
2. The normal full-scrape path may delete/replace pending rows from completed
   jobs. It has no supported preserve-all-existing-reviews switch. Triggering it
   against UEL's current production review would violate the explicit constraint
   not to overwrite staged reviews.

No production writes, deployments, approvals, job starts, cancellations, or
service restarts were performed. Queries ran in read-only database transactions.
An initial metadata query referenced a nonexistent `total` column, failed without
writing, and was corrected to use `current`.

## Independent verification completed

See [offline lifecycle results](uel-variant-lifecycle-tests.md):
**40 passed in 19.55 seconds**, including synthetic DB staging/approval and
route ownership, retry/resume/re-extraction, cancellation/deadline, eligibility,
and IELTS component tests. This does not substitute for full runtime verification.

## Remaining acceptance evidence

- Obtain release authorization or confirmation that the variant release is live.
- Choose an isolated verification database/run that cannot mutate the existing
  review; do not treat a targeted retry as a fresh full-discovery run.
- Verify released source and worker identity before launching.
- Run fresh undergraduate and postgraduate discovery and full route expansion.
- Record unique source-page counts, route counts, per-phase and total elapsed
  time, exclusions, unknown eligibility/template errors, and any timeouts.
- Reconcile route identities through actual staging and compare route-owned
  IELTS overall/component evidence against fresh source panels.
- Leave verification data unapproved and verify existing review rows are unchanged.