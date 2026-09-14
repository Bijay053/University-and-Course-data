# Task466 Macquarie production read-only verification

Initial evidence was generated 2026-09-14T07:12:12Z; the one-shot MQ follow-up
was checked at 2026-09-14T07:14:03Z through the dedicated SSM identity. A
second read-only runtime/log reconciliation was captured at approximately
2026-09-14T07:16:12Z, and the final status/staging query was captured at
2026-09-14T07:24:10Z (with the scoped staging-row detail at 07:24:42Z). The
structured evidence and complete earlier 180-URL list are in
`backend-py/docs/production/task466-mq-readonly-verification.json`.

## Safety boundary

- This was read-only verification only: no restart, deploy, scrape dispatch,
  configuration write, staging write, or database write was issued.
- Production queries loaded the external host's
  `/etc/university-portal/database.env` for the application connection and used
  `SELECT` statements only. This is the AWS-hosted external database, not a
  Replit database.
- SSM access used the dedicated `AWS_SSM_ACCESS_KEY_ID` /
  `AWS_SSM_SECRET_ACCESS_KEY` environment secrets. Values were never printed.
- The one-shot operational helper used for the follow-up was kept under
  `/tmp/task466_ssm_readonly.py` and is not a repository deliverable.
  Generalized wrapper behavior and regex checks are convenience safeguards, not
  a security boundary.

## Canonical Macquarie recipe and overrides

- Production university row: **Macquarie University**, `university_id=25`,
  `https://www.mq.edu.au`.
- Host slug derived by the production loader: **`mq`**.
- Exact selected recipe path on production:
  **`/opt/university-portal/backend-py/scraper_config/unis/mq.yaml`**
  (`backend-py/scraper_config/unis/mq.yaml` relative to the checkout).
- It is the shared recipe, not an ID-specific recipe. SHA-256 observed:
  `2b06f14f37831540eeccd2cd62e40f65e45d210c10f80ec40721f2d823b8ef39`.
- Effective `discovery.expected_min_courses` remains **300**,
  `skip_browser_discovery=true`, `always_sitemap_supplement=false`, and
  `failure_guard_threshold=0.30`.
- Effective extraction retains `scrape_do_render=true`, AUD fees,
  international/annual fee preference, `skip_degree_qualifier_check=true`,
  `require_international_fee=false`, and only `course_name` as a required
  staging field.
- The DB `scrape_config` has an auto-generated probe block (Cloudflare/proxy
  metadata, AUD default, `skip_browser_rescue=true`, and
  `use_stealth_browser=false`). No DB `admin_config` or legacy `uniPages`
  override was present.
- Effective precedence observed in the production loader is built-in defaults →
  `defaults.yaml` → DB `uniPages` translation → DB `auto_config` → `mq.yaml` →
  DB `admin_config` → locked recipe paths re-applied last. Since `admin_config`
  and `uniPages` are absent here, the 300-course floor is the canonical YAML
  value, not a hidden operator override.

## Jobs and existing terminal state

At verification time, the production queue was **not idle**:

| Runtime job | University | Status | Created/started | Progress |
|---|---|---|---|---|
| `job_4fd2b910eda0` | Curtin (`33`) | `running` | `2026-09-14T07:06:36.700195Z` | `current=44`, `total_found=246` |
| `job_257702eef633` | Macquarie (`25`) | `completed_with_warnings` | `2026-09-14T07:06:38.743766Z` | `current=183`, `total_found=183`, `imported=182`, `skipped=1` |

The earlier terminal Macquarie job `job_d4bcad741d9b`:

- `completed_with_warnings`, started `06:50:23Z`, completed `06:56:38Z`.
- `181` raw candidates → `181` extractable URLs → `180` staged rows.
- The initial terminal staging query reported `180` rows as `pending`; the
  final DB scope reports those earlier-job rows as `approved`. One course was
  skipped as `online_only=1`.
- `fetchFailed=0`, `errors=0`.
- Terminal line: `Found:181 | Staged:180 | Skipped:1 | FetchFailed:0 |
  Errors:0 | CatalogueFloor:300 | Raw:181`.
- The catalogue-floor guard explicitly reported
  `catalogue_below_expected_min` / `completed_with_warnings`; existing
  published and review data was not auto-deleted.
- Data-quality summary: `0 critical`, `2 warning`, `23 info`.
- Runtime release identity recorded on this job:
  `b07a55245a7c41886cbb1b15d84c562055195caa`.

The complete earlier-terminal URL list is in the JSON evidence. It contains
180 unique non-null canonical URLs; the final terminal query exposed only two
current-job durable rows and no complete 182-URL replacement list.

### Catalogue-audit handoff

The staged URL evidence path sent for `mq-catalogue-audit` is
`backend-py/docs/production/task466-mq-readonly-verification.json`; consume
the `current_url_list` key (180 URLs) and keep it attributed to terminal job
`job_d4bcad741d9b`. A shared handoff pointer is also available at
`/tmp/mq-catalogue-audit/task466-staged-url-evidence.path`. The final
Macquarie job's terminal metric reports 182 staged rows, but its direct
current-job table scope exposes only the two URLs documented below.

### One-shot outcome check of the already-running MQ job

At `2026-09-14T07:14:03Z`, one read-only SSM query targeted only the already
running `job_257702eef633`; it did not dispatch, restart, deploy, or mutate
anything. The job was **still `running`**:

- `completed_at=null`, `current=0`, `total_found=0`, `imported=0`,
  `skipped=0`, `errors=0`, and `error_message=null`.
- It retained release revision
  `b07a55245a7c41886cbb1b15d84c562055195caa`.
- Since it had not reached a terminal state, there is no new staging outcome
  or replacement URL list to report. The staged URL evidence below remains
  explicitly tied to terminal job `job_d4bcad741d9b`, not this still-running
  job.
- SSM command ID for the one-shot check:
  `b4dcb909-30a2-4956-b0ae-6515cb243a8d`.

### Latest MQ runtime status and source-record reconciliation

The earlier read-only query (approximately `2026-09-14T07:16:12Z`) checked the
same already-running `job_257702eef633`; it had not imported anything at that
point: `status=running`, `current=0`, `total_found=183`, `imported=0`,
`skipped=0`, `errors=0`, and `completed_at=null`. The final query below is the
terminal follow-up; neither query dispatched or restarted a job.

The external catalogue-audit accounting can now be reconciled without calling
the difference “retired routes”:

| Funnelback/source-accounting step | Count | Evidence/interpretation |
|---|---:|---|
| Raw Funnelback results | 372 | Production discovery log |
| Audit-accepted source rows | 212 | Independent accounting: 138 `Course` + 74 `Double Degree` |
| Production unique canonical candidates | 209 | Production log: `372 → 209`; year-stamped routes are canonicalized |
| Canonical candidates with dual origin 404 proof | 28 | Exact records in `/tmp/task466_mq_exact_exclusions.json` |
| Page-data-enriched candidates | 181 | Production log: `209 - 28` |
| `online_only` rejection | 1 | Graduate Diploma of Applied Finance |
| Terminal staged rows | 180 | `181 - 1` |

The URL-level crosswalk over the captured Funnelback JSON found exactly three
accepted source-row duplicates after the production year-route canonicalizer
removed `/courses/2026/`:

1. Bachelor of Commerce and Bachelor of Psychology: unversioned
   `.../bachelor-of-commerce-and-bachelor-of-psychology` and 2026
   `.../2026/bachelor-of-commerce-and-bachelor-of-psychology`.
2. Bachelor of Science and Bachelor of Engineering (Honours): unversioned
   `.../bachelor-of-science-and-bachelor-of-engineering-honours` and 2026
   `.../2026/bachelor-of-science-and-bachelor-of-engineering-honours`.
3. Bachelor of Information Technology and Bachelor of Engineering (Honours):
   unversioned
   `.../bachelor-of-information-technology-and-bachelor-of-engineering-honours`
   and 2026
   `.../2026/bachelor-of-information-technology-and-bachelor-of-engineering-honours`.

Thus the apparent `212 - 180 = 32` source-row difference is exactly
`3` canonical duplicate source rows + `28` dual-404 exclusions + `1`
`online_only` rejection. The three pairs are duplicate year routes, not
independent evidence that three courses were retired. The scoped terminal
log/journal query found **zero** messages matching retired, stale,
route-exclusion/filter, or year-stamped terminology; the production
`372 → 209` message is only an aggregate filter count. The exact 28 URLs,
timestamps, sequence numbers, and full dual-origin-404 proof messages are in
`/tmp/task466_mq_exact_exclusions.json`; the scoped DB and journal evidence
is in `/tmp/task466_mq_exclusions.json`.

### Final terminal MQ status and staging capture

At `2026-09-14T07:24:10Z`, a final read-only SSM query (command
`f842634c-8c61-4a0c-b2ed-73c6e87f3963`) found that the previously running
Macquarie job had reached a terminal state:

- `job_257702eef633`: `completed_with_warnings`, completed at
  `07:17:07.803637Z`, `current=183`, `total_found=183`, `imported=182`,
  `skipped=1`, `errors=0`.
- The terminal capture reports `Found:183 | Staged:182 | Skipped:1
  (online_only=1) | FetchFailed:0 | Errors:0 | CatalogueFloor:300 | Raw:183`.
- The catalogue guard remained `catalogue_below_expected_min`; the terminal
  error says 183 raw/extractable candidates produced 182 staged courses
  against the configured minimum of 300.

The scoped staging-row detail query completed at `2026-09-14T07:24:42Z`
(SSM command `40e24534-b00c-4f5e-849c-58ea51d31158`). Its direct
`scraped_courses WHERE scrape_job_id='job_257702eef633'` result was **2**
pending rows, with two unique URLs:

| Course | URL | Category | Degree |
|---|---|---|---|
| Master of Engineering (Professional) in Mechanical Engineering | `mq.edu.au/study/find-a-course/courses/master-of-engineering-professional-in-mechanical-engineering` | Engineering & Technology | Master's |
| Master of Environment and Master of Sustainable Development | `mq.edu.au/study/find-a-course/courses/master-of-environment-and-master-of-sustainable-development` | Agriculture & Environmental Science | Master's |

The direct row-scope counts were `pending=2`, `url_count=2`,
`unique_url_count=2`; category counts were one each for the two categories
above, and the degree count was `Master's=2`. The same read-only query found
the earlier `job_d4bcad741d9b` rows as `approved=180`. This is deliberately
reported alongside, rather than substituted for, the runtime terminal metric
`imported/staged=182`: the DB query does not expose 182 current-job rows, so
the evidence must not claim a complete 182-URL durable row list. The complete
180-URL list in this report remains tied to the earlier terminal job
`job_d4bcad741d9b`.

### Earlier-terminal staging breakdown (`job_d4bcad741d9b`)

Categories:

| Category | Count |
|---|---:|
| Business & Management | 70 |
| Arts, Humanities & Social Sciences | 26 |
| Medicine & Health | 17 |
| Other | 13 |
| Computer Science & IT | 12 |
| Law & Legal Studies | 12 |
| Education & Social Work | 10 |
| Engineering & Technology | 8 |
| Media & Communications | 7 |
| Science & Mathematics | 3 |
| Agriculture & Environmental Science | 2 |

Degree levels:

| Degree level | Count |
|---|---:|
| Master's | 75 |
| Bachelor's | 74 |
| Diploma | 8 |
| Graduate Certificate | 8 |
| Doctorate | 7 |
| Graduate Diploma | 6 |
| Unknown | 2 |

This run does include research offerings and combined-degree titles in the
URL list. The evidence does **not** independently reconcile the official
current international catalogue, so it does not justify lowering the 300
floor or declaring any absent category genuinely missing. The warning should
remain until the authoritative public/page-data reconciliation required by the
task is completed.

## Existing production terminal/runtime state

- Host: `ip-172-31-40-159.ap-south-1.compute.internal`.
- `uni-api-py.service`: active/running, main PID `1004053`, active since
  `2026-09-14 06:50:04 UTC`.
- `uni-celery.service`: active/running, main PID `1004105`, active since
  `2026-09-14 06:50:07 UTC`.
- `/opt/university-portal/backend-py/.release.env` exists and records revision
  `b07a55245a7c41886cbb1b15d84c562055195caa`.
- The production package has no usable `.git` metadata (`HEAD` and origin were
  unavailable), so repository identity cannot be independently compared on
  the host; the release file and runtime job identity are the available
  release evidence.
- API `/api/health` returned `{"status":"ok"}` and Celery inspect ping returned
  one online node with `pong`.

## Signed idle-restart gate readiness

**Not ready; no restart was attempted.**

1. Curtin remained `running` at the final evidence capture; the fresh
   Macquarie job reached `completed_with_warnings` but remained below its
   configured catalogue minimum.
2. The signed proof, template, and signer files exist, but the independent
   `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID` setting was not configured. The
   checked-in proof validator therefore failed closed with:
   `blocked: independent expected rehearsal account is not configured`.
3. The sample ordinary-HTML check returned `403|text/html`, not the required
   `200` HTML response.
4. API health and Celery ping passed, and both systemd services were active.

The checked-in gate must be re-evaluated only after the active job drains and
the independent expected-account setting is provisioned. Do not infer
readiness from the presence of the proof file alone. No deploy was made:
lowering300 remains unjustified by the available evidence and this gate
blocker must be reported rather than bypassed.