# Task467 Macquarie MPhil omission follow-up

This follow-up records the bounded diagnosis and minimal repair for the
Macquarie production run `job_e8ecd007ab8a` on release
`7d13d75ed92c35966493c082705007b5925a8002`.

## Observed omission

The run reached `completed_with_warnings`, with:

- `190` raw and extractable candidates;
- `189` staged rows, all still `pending`;
- one staged `Doctor of Philosophy` row;
- no staged `Master of Philosophy` row;
- the unchanged catalogue floor of `300`.

The persisted targeted runtime-log query (SSM command
`15e2812c-ab59-4c67-ae5b-83556d10b2f9`) recorded:

```text
[DISCOVER] MQ: research authority did not verify Master of Philosophy;
leaving it out rather than staging an unverified row
```

This is a safe omission, not evidence that the qualification is absent from
Macquarie's authority source.  A bounded live probe (SSM command
`b16c760c-84ee-41bf-a78b-c58b12851cf8`) observed a transient rendered
provider failure for the PhD source (`ROTATION_FAILED`, HTTP 502) and a
successful 20,392-byte MPhil source with valid authority evidence.  Direct
origin requests to both research pages returned a Cloudflare challenge (HTTP
403), so direct HTTP is not a valid production authority transport.

## Minimal repair

`_fetch_mq_research_source` used `fetch_html_scrape_do(..., max_retries=0)`.
Consequently, one transient rendered-provider failure could make one
qualification fail closed while the other qualification was staged.  The
helper now requests exactly one bounded provider retry
(`max_retries=1`).  It still:

1. rejects challenge shells;
2. falls back to direct HTTP only as a secondary transport;
3. requires exact qualification title, full-time duration, and
   international-student evidence;
4. requires the central research-fee authority before staging a metadata-only
   row; and
5. never copies a domestic/default fee into `international_fee`.

The 300-course catalogue floor and its warning/terminal guard are unchanged.

## Regression

`tests/test_mq_research_authority.py` now asserts that the authority fetch
requests one provider retry while preserving the existing exact-title,
duration, international-evidence, route-identity, fee-safety, and two-row
staging regressions.

Focused local regression run on the merged primary worktree:

```text
137 passed, 1 skipped
tests/test_mq_research_authority.py
tests/test_mq_coursehandbook_sitemap.py
tests/test_mq_coursehandbook_to_admissions.py
tests/test_stage_evidence_and_review.py
```

## Required post-deploy evidence

After this repair is deployed, one fresh `forceDiscovery=true` MQ run must
record both current-run rows before this follow-up is closed:

- `Doctor of Philosophy`: `Doctorate`, `3.00 year`, `Full Time`;
- `Master of Philosophy`: `Master's`, `2.00 year`, `Full Time`;
- both rows `status=pending`;
- both rows with selected authority evidence for title, duration, full-time,
  international evidence, and the central fee source;
- both rows with `international_fee=NULL` unless an explicitly labelled
  international amount is returned by the current-route page-data; and
- the catalogue floor remains `expected_min_courses=300`, even if the run
  remains `completed_with_warnings` below that floor.

No approval, deletion, baseline reduction, or stale-row reuse is part of this
verification.
