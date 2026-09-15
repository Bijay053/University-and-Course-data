# Task467 Macquarie research parser and staging validation

This is a local, read-only validation record for the Task467 PhD/MPhil
authority supplement. It does not dispatch a scrape, restart a service,
change a production row, or write to the production database. The capture was
run at **2026-09-14T07:54:43Z**.

## Transport and official-source result

The three configured sources were requested directly over HTTPS with
redirects enabled and a 30-second timeout:

| Source | HTTP result | Content type | Bytes | SHA-256 | Parser result |
|---|---:|---|---:|---|---|
| [Doctor of Philosophy](https://www.mq.edu.au/research/phd-and-research-degrees/explore-research-degrees/doctor-of-philosophy) | 403 | `text/html; charset=UTF-8` | 5,684 | `d65e96219b7ee76cc6419b2003921533c99ab276dfa629cf684f3cdd2ff1f52d` | rejected as challenge |
| [Master of Philosophy](https://www.mq.edu.au/research/phd-and-research-degrees/explore-research-degrees/master-of-philosophy) | 403 | `text/html; charset=UTF-8` | 5,684 | `d0349fceb5efb54318db17a671053f77b569fc2e3072fdb89c73a5491bf57842` | rejected as challenge |
| [Research-student fees](https://www.mq.edu.au/study/admissions-and-entry/fees-and-costs/research-students) | 403 | `text/html; charset=UTF-8` | 5,581 | `df5bd0659304857b9212ee58d90bc053ee137df7f7fcecbf5c7ea21e34824c71` | rejected as challenge |

Each response had the Cloudflare challenge title `Just a moment...`. The
challenge-shell detector therefore returns no authority evidence; the parser
does not treat a 403 shell as a course page or as fee evidence. A text proxy
was also checked, but it returned a 200 wrapper containing the origin's 403
warning and was not accepted as official source content.

The local parser check against those exact downloaded response bodies returned:

```text
Doctor of Philosophy challenge_shell=True parser_result=None
Master of Philosophy challenge_shell=True parser_result=None
research-fees challenge_shell=True parser_result=None
```

The source URLs and the positive excerpts below are the official Macquarie
captures committed in
[`task466-mq-research-authority.md`](task466-mq-research-authority.md). This
run verifies that the new parser still accepts those explicit source results
while failing closed for the fresh live challenge responses; it does not claim
that the current direct 403 response is positive live-course evidence.

## Positive parser validation from official captured excerpts

The validation vectors use the exact authority facts previously captured from
the official pages:

- `Doctor of Philosophy` — `Three years full-time equivalent`; time
  commitment is full time; the page mentions international students.
- `Master of Philosophy` — `Two years full-time equivalent`; time commitment
  is full time; the page mentions international students.
- The official research-fees excerpt says that an international student on a
  student visa must pay international fees.

The fixture hashes and parser outputs were:

| Qualification | Fixture SHA-256 | Authority evidence | Classification | Duration | Study load | International fee |
|---|---|---|---|---:|---|---|
| Doctor of Philosophy | `93f2d935c840711047dcfa55c5badf182e42dc8df5e941fa7d40f4b23efa94d9` | `true` | `Doctorate` / `Doctorate` | `3.0 year` | `Full Time` | `null` |
| Master of Philosophy | `af85512ad63617715a97bdc999b0786edc11afc7c35eb2607e1e5e3988d56649` | `true` | `Master's` / `Postgraduate` | `2.0 year` | `Full Time` | `null` |

Both rows also produced:

- exact qualification classification (`Doctorate`/`Doctorate` for PhD and
  `Master's`/`Postgraduate` for MPhil);
- `international_full_time_source_verified=true`;
- `has_central_fee_page=true` only when the research-fees authority evidence
  was present;
- an explicit `research_fee_source_url`;
- no domestic fee copied into `international_fee`.

The selected evidence methods were:

```text
mq:research_authority_title
mq:research_authority_full_time_duration
mq:research_authority_full_time
mq:research_authority_international_evidence
mq:research_fee_authority
```

The parser rejects missing international-student evidence, a part-time-only
duration, a mismatched numeric duration, a non-exact page title, a
Cloudflare challenge shell, an unrelated first admissions link, and
page-data whose normalized qualification title does not exactly match the
authority row. In the latter case it keeps only authority-backed metadata and
discards the unrelated page-data fee.

## Local staging verification

Both built qualification payloads were passed through actual `_extract_only`
scrapy-result short-circuits and then through the actual `stage_course`
function using the local SQLAlchemy test database and Macquarie's local
configuration. This is a staging-path check, not only a discovery check:

```text
RESULT saved= True reason= staged id= 193787
ROW name= Doctor of Philosophy status= pending degree_level= Doctorate duration= 3.00 international_fee= None fee_term= None review= None
CLEANUP deleted_local_row= 193787
```

The temporary row used university id `277` (Macquarie University) and was
deleted and committed in the same local test run. No production database was
used or modified. The `has_central_fee_page` flag allowed the verified
metadata-only row to enter review without fabricating an annual fee; the
stored fee remained `NULL`.

The automated two-qualification integration regression additionally asserts
for both rows that `_extract_only` returns the exact `scrapy_result`, staging
succeeds, `international_fee` and `fee_term` persist as `NULL`, duration and
degree classification persist correctly, `study_load` is `Full Time`, and
authority international/full-time/fee-source evidence rows are persisted.

The broader integration staging test module also passed:

```text
tests/test_stage_evidence_and_review.py  10 passed
```

Focused MQ and transport validation after the staging-path change:

```text
tests/test_mq_research_authority.py  12 passed
tests/test_mq_coursehandbook_sitemap.py
tests/test_macquarie_stealth_browser.py
tests/test_stage_evidence_and_review.py  10 passed
```

The combined focused run after the adversarial route/title and coverage
regressions was:

```text
156 passed, 1 skipped
```

The coverage regressions use five candidates rather than asserting threshold
constants: three page-data successes fail the `3/5` page-data guard before a
successful research supplement can run, and three international-fee successes
fail the `3/5` fee guard before that supplement can run.

## Operational limits

- The direct official pages were Cloudflare-challenged from this local
  transport, so a live positive fetch was not asserted. Production's
  configured rendered transport remains the verified path.
- `scrape_do_render: true` remains enabled in `scraper_config/unis/mq.yaml`;
  the current transport setting was intentionally preserved.
- No production scrape, staging write, restart, or release gate was attempted.
- Production validation is blocked independently by the active SEGi job and
  by the missing independently configured
  `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID`. The signed rehearsal proof cannot
  be accepted without that independent expected account, and the idle gate
  cannot pass while SEGi is running.
