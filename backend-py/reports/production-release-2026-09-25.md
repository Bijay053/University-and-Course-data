# Production application release — 2026-09-25 UTC

## Revision and release outcome

- Production predecessor: `0234eb631c05bb2f0b3099f3767683c9cbea6567`
- Reviewed application target: `4ce0342b088f47cf022208e1b65fa3750178484c`
- The workspace was clean at candidate freeze. The verified production Git
  origin's `main` tip was exactly the target, and a non-force push from the
  workspace reported it was already up to date. The production checkout was a
  descendant-safe fast-forward from the predecessor.
- The existing `backend-py/deploy/guarded_release.sh` was invoked on the
  production host with those two full revisions and the independently verified
  disposable-account identity. AWS Systems Manager command
  `97444f54-49ce-436c-8fcb-c43b3fa3fede` finished
  **Success** and emitted
  `DEPLOYED_RELEASE=4ce0342b088f47cf022208e1b65fa3750178484c`.
  No release guard was bypassed.

## Prerequisites and preservation

- The prior signed database-refresh rehearsal receipt was stale. An authorized
  disposable-account rehearsal completed, transferred a new signed receipt,
  and independently verified teardown of its own tagged stack and residue.
  Production validated the new receipt against its pinned signer and
  independent account setting before release.
- Before application cutover, the production Alembic ledger was at
  `385_dated_review_history` and the reviewed campus-fee migration's column
  and index were absent. A root-only backup of the affected course table and
  migration ledger was made on the host at
  `/var/lib/university-portal/release-pre386-scraped-courses-20260925.dump`.
  Migration
  `386_campus_fee_scope` was applied from the exact target Git blob in one
  database transaction, including the ledger update, after checking that
  there were no active jobs or duplicate staged identities. Final read-only
  checks found the new column and unique index and ledger revision 386.
- The guarded reconciliation verified and restored four operator-edited
  tracked recipes: `canterbury_1759.yaml`, `law_1902.yaml`,
  `londonmet.yaml`, and `mdx.yaml`. Its read-only redundant-overlay audit
  reported zero; generated settings and unrelated runtime files were not
  cleaned. No course approvals or record rewrites were performed as part of
  this release. The guard's required safe-restart smoke ran before cutover.

## Activation and independent verification

- The guarded restart reported exact API and Celery release identities at the
  target. A separate read-only post-release check confirmed that production
  Git `HEAD`, fetched `origin/main`, `.release.env`, and both running service
  process environments all matched the full target revision.
- Both services were active. `http://127.0.0.1:8000/api/health` returned
  `{"status":"ok",...}`. The worker was consuming the `scrape` queue,
  with zero active, reserved, or scheduled tasks; the production database
  reported zero queued, running, or awaiting-approval scrape jobs at the
  final check.
- The guarded frontend verifier confirmed the built release marker, public
  portal HTML, SPA fallback, hashed assets, and cache policy. Its public
  result was `FRONTEND_RELEASE_OK /assets/index-DJ4fBIXR.js`; the public
  HTML referenced the newly published asset containing the target marker.
- Focused local checks before release passed: 120 release/approval/migration
  tests, 56 approval/fee/route tests, 36 isolated fee/migration tests,
  University Portal typecheck and build, and 192 frontend tests. Completion
  validation also passed the full scraper regression: 5,210 passed,
  27 skipped, 30 deselected, plus the 22-test catalogue review check.

**Blockers or incomplete production checks:** None. Source-map and bundle-size
warnings from the frontend build were nonfatal. The release revision is the
target above; this post-release report is documentation, not a second
application release.
