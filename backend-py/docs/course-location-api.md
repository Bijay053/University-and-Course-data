# Course locations for external application portals

One published course has one stable integer `id`. Campuses are not separate
courses. Use the parent course ID when creating an application in your portal.
This repository does not implement the external application workflow.

## Course catalogue

`GET /api/courses` returns `data[]`; `GET /api/courses/{id}` returns a course.
Both add:

```json
{
  "id": 123,
  "name": "MSc Healthcare Management",
  "locations": ["Birmingham", "Leeds", "London", "Manchester"],
  "offerings": [
    {"id": "456", "location": "London"}
  ]
}
```

The example offering is abbreviated: responses include every persisted offering.
`offerings[].id` is a stable string identifier for that parent's campus.
These fields are read-only, contain **no tuition**, and require no fee choice.
Existing `course_location` / list `courseLocation` and preexisting fee fields
remain compatible. Legacy courses without verified offering records return
empty `locations` and `offerings`; retain their existing scalar location display.
Do not infer verified campus choices by splitting arbitrary legacy location text.

Ordinary course PATCH preserves offering records. Attempts to change the award,
route, degree or location of a verified-offering course return HTTP 409; those
changes require evidence review. Offering payloads are not accepted by PATCH.

## Course Search only

`GET /api/search/courses` returns `results[]`, one result per course ID, with:

```json
{
  "id": 123,
  "offerings": [
    {
      "id": "456", "location": "London",
      "feeAmount": 19050, "feeCurrency": "GBP",
      "feeTerm": "Full Course", "feeYear": 2026
    }
  ]
}
```

Each fee field is nullable. All returned offerings belong to the result's parent
course. Search combines location/city and fee bounds on the **same offering**,
then returns every offering of the matching course, not just the matched campus.
Legacy courses retain existing scalar filtering behavior and an empty array.
Different campus fees are not promoted into a misleading single course-wide fee.

The parent identity does not include tuition year or billing period. A verified
fee-year rollover preserves both the parent course ID and campus offering IDs.
Changing year, period or currency requires all previously published campuses in
one validated, atomic approval cohort. Partial rollovers, mixed cohorts and
older-year overwrites fail review rather than blending incompatible tuition.
Exact source routes (including semantic year/query selectors), award and study
variant remain identity boundaries.

## Migration and reconciliation

Alembic revision `387_course_offerings` is additive and does not modify published
records. Apply it before serving the new code. No deployment is performed here.
`scripts/apply_migration_387_local.py` is a guarded local-development helper: it
verifies loopback or Replit's private PostgreSQL service, applies only this
additive DDL, and advances revision tracking only from its exact parent 386.
It refuses nonlocal targets and never upgrades unrelated Alembic heads.

Preexisting published campus duplicates are deliberately not merged or deleted.
Approval blocks those identities pending reconciliation. A safe reconciliation
requires a read-only inventory grouped by university, exact award, source route,
fee year/period and study variant; verify source evidence and external references;
choose a parent ID with the application portal owner; back up all affected rows;
approve an explicit old-ID-to-parent/offering mapping; then run a separately
reviewed transactional migration with audit records and compatibility redirects.
Never group different awards solely by URL. This change supplies no destructive
reconciliation command. Existing offering removal also requires explicit review.