# Task466 Macquarie catalogue reconciliation

Generated 2026-09-14 from an independent read-only public-catalogue
comparison. This report is supplementary to
`task466-mq-readonly-verification.md`; it does not alter the production
configuration or dispatch another scrape.

The machine-readable canonical ledger is committed in
`task466-mq-catalogue-reconciliation.json`. Research-authority excerpts and
source URLs are committed in `task466-mq-research-authority.md`; the core
decision does not depend on a `/tmp` artifact.

## Decision

**Do not lower `discovery.expected_min_courses` from 300.** The official
international profile returned 212 pre-enrichment records, but that is not the
current canonical course count. After the repository's current-route
canonicalisation and deduplication, there are 209 candidates. Production
logs then record 28 candidates with dual rendered 404 confirmation
(`page-data.json` and public detail) and one live course rejected by the
`online_only` staging rule:

```text
212 Funnelback degree records
→ 209 canonical URLs (three year-route aliases deduplicated)
→ 181 candidates after 28 dual-404 retirements
→ 180 staged after one online_only skip
```

This reconciles the terminal 180 without calling the 212 pre-enrichment
records missing. It does not establish that 300 is too high. The floor must
remain a warning until the authoritative research catalogue and any other
source categories outside the international Funnelback profile are reconciled.

This report is therefore a baseline audit, not a source-repair proposal.

### Superseding fresh terminal check

The later job `job_257702eef633` finished `completed_with_warnings`, with
183 validated candidates, 182 reported staged, one `online_only` skip,
zero fetch failures and zero errors; the floor remained 300. The direct
current-job staging query returned two pending rows, while the earlier 180
rows were now approved. The reported 182 is therefore not independently
verified as 182 new rows owned by the later job. See the companion production
report for the exact scopes and observations.

The two current-job rows are Master of Engineering (Professional) in
Mechanical Engineering and Master of Environment and Master of Sustainable
Development. Both had dual-404 exclusions in the earlier run. Accordingly,
the earlier ledger below records **observed origin-not-found exclusions,
not proof of permanent course retirement**. This source variability is an
additional reason not to establish a lower baseline from one successful
provider pass. No runtime configuration was changed or deployed by this audit.

## Production baseline being compared

The companion production evidence records terminal job
`job_d4bcad741d9b`:

- 181 raw candidates and 181 extractable URLs;
- 180 staged rows, all pending;
- one `online_only` skip;
- zero fetch failures and zero extraction errors;
- explicit `catalogue_below_expected_min` warning at the configured floor of
  300.

The earlier 180-row terminal set is the fixed comparison snapshot for the
arithmetic below; the fresh terminal check above supersedes the intervening
running snapshot without rewriting that historical ledger.

The complete 180-URL production list is committed in
`task466-mq-readonly-verification.json`. URL comparison normalises the
`www.mq.edu.au` versus `mq.edu.au` host spelling and removes a trailing slash;
it does not apply title or slug matching. The resulting set arithmetic is:

| Set / transition | Count |
|---|---:|
| Terminal production canonical URLs | 180 |
| Raw international `Course`/`Double Degree` records | 212 |
| Canonical URLs after year-route deduplication | 209 |
| Canonical candidates after dual 404 retirement | 181 |
| Canonical candidates after `online_only` skip | 180 |

The 28 dual-404 URLs and the one online-only URL are listed in the committed
canonical source ledger below. The exact terminal proof template and sequence
numbers for all 28 records are also committed in
`task466-mq-terminal-dual-404.json`. This ledger is based on the terminal
production discovery messages, not on a title-only comparison.

## Independent source and reproducible method

The source is Macquarie's official Funnelback course collection, queried with
the international profile:

```text
https://mqu-search.funnelback.squiz.cloud/s/search.json?collection=mqu~sp-courses&profile=international&query=!padrenull&start_rank=1&num_ranks=200
https://mqu-search.funnelback.squiz.cloud/s/search.json?collection=mqu~sp-courses&profile=international&query=!padrenull&start_rank=201&num_ranks=200
```

The direct MQ course-handbook origin was returning a CloudFront 403 from the
verification network. The official Funnelback endpoint was therefore fetched
through the read-only `r.jina.ai` text proxy; the JSON `URL Source` in each
response remains the official Funnelback URL above. This is a transport
workaround, not a substitute catalogue.

The two responses reported `fullyMatching=372`, with 200 and 172 rows
respectively. The response fields used were `liveUrl`, `title`,
`listMetadata.courseType`, `listMetadata.studyLevel`,
`listMetadata.courseCode`, and `listMetadata.internationalCourse`. The
responses contained 372 unique URLs:

| Official profile result type | Count |
|---|---:|
| `Course` | 138 |
| `Majors and Specialisations` | 160 |
| `Double Degree` | 74 |
| **Total** | **372** |

The pre-enrichment comparison set is exactly the 138 `Course` plus 74
`Double Degree` records (212). It excludes the 160 records whose metadata identifies
them as majors/specialisations or whose URL shape is a year-prefixed
major/specialisation sub-page. This mirrors
`app/services/scraper/mq_browser_discover.py`:

- `_COURSE_PATH_RE` permits one or two path segments after the level token;
- `_BLOCKED_PATH_SUBSTRINGS` rejects `/major/`,
  `/specialisation/`, `/specialization/`,
  `/postgraduate-specialisation/`, and
  `/undergraduate-specialisation/`;
- the 10 year-prefixed sub-degree records fail the path-shape check even
  though the block substring is not adjacent to `/courses/`.

The 160 excluded records break down as follows:

| Exclusion reason | Count |
|---|---:|
| `/courses/major/<slug>` | 69 |
| `/courses/specialisation/<slug>` | 37 |
| `/courses/postgraduate-specialisation/<slug>` | 38 |
| `/courses/undergraduate-specialisation/<slug>` | 6 |
| Three-segment year-prefixed sub-degree path (9 major, 1 postgraduate specialisation) | 10 |
| **Total excluded** | **160** |

The raw proxy responses used for this report were retained during verification
and hashed before analysis:

| Response | SHA-256 |
|---|---|
| `start_rank=1` text response | `08f3b28c7454237b8b874a61cb1ca6bf082cdcda9b5c0a63e2a1383074ca1c82` |
| `start_rank=201` text response | `564659b05d7ba815e7567ec2f34f235bb38fb64df2c55ea2dff5811215926f22` |
| `start_rank=1` parsed JSON | `0e974c22f1970a65977bd4e1bb642fffc03cc888e53809a2eb38866fad9fc727` |
| `start_rank=201` parsed JSON | `fc8165a610aed381982fc1d5012bc2652dc977d475c14a14612d18440d3a6761` |

## Canonical source ledger for the 29 non-staged candidates

The raw 212 records are not 212 current routes. Three records are duplicate
year-stamped aliases of an unversioned route and collapse during
canonicalisation. Of the resulting 209 URLs, the terminal production logs
explicitly record 28 dual 404 retirements and one current route that was
successfully discovered but skipped as `online_only`. Course codes make this
ledger independent of title spelling.

The three alias pairs are:

| Course code | Title | Unversioned route | Year-versioned alias |
|---|---|---|---|
| C000492 / D000073 | Bachelor of Commerce and Bachelor of Psychology | `/courses/bachelor-of-commerce-and-bachelor-of-psychology` | `/courses/2026/bachelor-of-commerce-and-bachelor-of-psychology` |
| C000486 / D006302 | Bachelor of Science and Bachelor of Engineering (Honours) | `/courses/bachelor-of-science-and-bachelor-of-engineering-honours` | `/courses/2026/bachelor-of-science-and-bachelor-of-engineering-honours` |
| C000487 / D000058 | Bachelor of Information Technology and Bachelor of Engineering (Honours) | `/courses/bachelor-of-information-technology-and-bachelor-of-engineering-honours` | `/courses/2026/bachelor-of-information-technology-and-bachelor-of-engineering-honours` |

`dual 404` means the terminal discovery log says both the canonical public
detail route and its Gatsby `page-data.json` route returned rendered origin
404. It is not inferred from the Funnelback count. The online-only status is
the terminal staging skip reason. The committed proof file records all 28
sequence numbers and the common terminal proof message.

| Course code | Title | Type | Terminal disposition | Canonical URL |
|---|---|---|---|---|
| C000003 | Bachelor of Environment | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-environment |
| D000065 | Bachelor of Commerce and Bachelor of Engineering (Honours) | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-commerce-and-bachelor-of-engineering-honours |
| D000047 | Bachelor of Environment and Bachelor of Laws | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-environment-and-bachelor-of-laws |
| C000008 | Bachelor of Planning | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-planning |
| C000088 | Graduate Certificate of Editing and Electronic Publishing | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/graduate-certificate-of-editing-and-electronic-publishing |
| C000139 | Graduate Certificate of Early Childhood | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/graduate-certificate-of-early-childhood |
| C000431 | Graduate Certificate of Health Leadership | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/graduate-certificate-of-health-leadership |
| C000069 | Graduate Diploma of Environment | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/graduate-diploma-of-environment |
| C000001 | Bachelor of Biodiversity and Conservation | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-biodiversity-and-conservation |
| C000058 | Graduate Diploma of Conservation Biology | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/graduate-diploma-of-conservation-biology |
| D000119 | Master of Business Analytics and Master of Information Systems Management | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-business-analytics-and-master-of-information-systems-management |
| D000107 | Master of Criminology and Master of Cyber Security Analysis | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-criminology-and-master-of-cyber-security-analysis |
| C000063 | Master of Conservation Biology | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-conservation-biology |
| C000399 | Master of Engineering (Professional) in Environmental Engineering | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-engineering-professional-in-environmental-engineering |
| C000397 | Master of Engineering (Professional) in Mechanical Engineering | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-engineering-professional-in-mechanical-engineering |
| C000401 | Master of Engineering (Professional) in Mechatronics and Automation Engineering | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-engineering-professional-in-mechatronics-and-automation-engineering |
| C000398 | Master of Engineering (Professional) in Civil and Construction Engineering | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-engineering-professional-in-civil-and-construction-engineering |
| C000400 | Master of Engineering (Professional) in Renewable Energy and Electrical Engineering | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-engineering-professional-in-renewable-energy-and-electrical-engineering |
| D005887 | Master of Environment and Master of Sustainable Development | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-environment-and-master-of-sustainable-development |
| C000023 | Master of Environment | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-environment |
| C000430 | Master of Health Leadership | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-health-leadership |
| C000372 | Master of Information Technology in Artificial Intelligence | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-information-technology-in-artificial-intelligence |
| C000125 | Master of Information Technology in Cyber Security | Course | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-information-technology-in-cyber-security |
| D000121 | Master of International Relations and Master of Public and Social Policy | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-international-relations-and-master-of-public-and-social-policy |
| D000149 | Master of International Relations and Master of International Trade and Commerce Law | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-international-relations-and-master-of-international-trade-and-commerce-law |
| D000131 | Master of Intelligence and Master of Public and Social Policy | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-intelligence-and-master-of-public-and-social-policy |
| D000120 | Master of International Trade and Commerce Law and Master of Public and Social Policy | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-international-trade-and-commerce-law-and-master-of-public-and-social-policy |
| D000104 | Master of Public and Social Policy and Master of Security and Strategic Studies | Double Degree | dual 404 | https://www.mq.edu.au/study/find-a-course/courses/master-of-public-and-social-policy-and-master-of-security-and-strategic-studies |
| C000152 | Graduate Diploma of Applied Finance | Course | live; online_only skip | https://www.mq.edu.au/study/find-a-course/courses/graduate-diploma-of-applied-finance |

## Research-category authority and exact-title gap

The international Funnelback profile returned only 10 research-labelled
course records (`internationalCourse=true`): four `Research Master` records
and six postgraduate graduate certificate/diploma records:

- Master of Research in Arts — C000408
- Master of Research in Business — C000413
- Master of Research in Science and Engineering — C000403
- Master of Research in Medicine, Health and Human Sciences — C000416
- Graduate Diploma of Research in Arts — C000409
- Graduate Diploma of Research in Business — C000412
- Graduate Certificate of Research in Business — C000411
- Graduate Diploma of Research in Science and Engineering — C000406
- Graduate Diploma of Research in Medicine, Health and Human Sciences — C000415
- Graduate Certificate of Research in Medicine, Health and Human Sciences — C000414

That profile is not an exhaustive research authority. The official research
degree index
(`https://www.mq.edu.au/research/phd-and-research-degrees/explore-research-degrees`)
links both of the following pages, neither of which appears in the production
180-URL list by exact title:

| Official research degree | Authority URL | Evidence relevant to international coverage |
|---|---|---|
| Doctor of Philosophy | https://www.mq.edu.au/research/phd-and-research-degrees/explore-research-degrees/doctor-of-philosophy | Three years full-time equivalent; scholarships may include OSHC and/or visa costs for international students |
| Master of Philosophy | https://www.mq.edu.au/research/phd-and-research-degrees/explore-research-degrees/master-of-philosophy | Two years full-time equivalent; scholarships may include OSHC and/or visa costs for international students |

The same authority's research-fees page says international research students
pay international fees:
`https://www.mq.edu.au/study/admissions-and-entry/fees-and-costs/research-students`.
The evidence for these pages was independently captured on 2026-09-14 in
the read-only research-authority check. This is stronger authority than the
international Funnelback profile for research coverage.

The terminal production rows labelled `Doctorate` are not an adequate
research check: exact titles include taught professional doctorates (Doctor
of Medicine, Doctor of Physiotherapy, Juris Doctor) and the four
faculty-specific Masters of Research. They do not include Doctor of
Philosophy or Master of Philosophy. Therefore the research source category
has a genuine exact-title omission from the 180 staged rows, even though no
new provider variant should be invented in this baseline audit.

The two handbook research certificates absent from Funnelback
(Graduate Certificate of Research in Arts, C000410, and Graduate Certificate
of Research in Science and Engineering, C000405) remain **unresolved**.
Domestic-default rendering and a missing international-profile result do not
prove that either is domestic-only; no such classification is made here.

The production 180-URL list does include some research and combined-degree
titles. Consequently, research and combined-degree support cannot be
considered reconciled merely because some such rows are present.

## Category conclusions and next action

1. Keep the 300 floor and the catalogue warning. The 212 is a pre-enrichment
   Funnelback count, not an authoritative current catalogue count. The
   reconciled terminal path is `212 → 209 → 181 → 180`.
2. Do not count the 160 majors/specialisations as missing degree courses;
   they are pre-enrichment source records excluded by the repository policy.
   They are present in the official source but explicitly excluded by the
   repository's MQ URL policy.
3. Treat the 28 dual-404 rows as origin-not-found exclusions in that run and the one
   `Graduate Diploma of Applied Finance` row as a live `online_only` staging
   exclusion, based on the terminal logs. Do not call these 29 a source gap
   based on raw counts, or infer permanent retirement: two returned in the
   later run.
4. Record PhD and MPhil as an authoritative research-category omission by
   exact title. This audit does not add a provider or repair the source.
5. Keep the two research certificates unresolved rather than classifying them
   from domestic-default HTML.
6. Re-evaluate the signed production gate only after the active jobs drain and
   `DATABASE_REFRESH_REHEARSAL_ACCOUNT_ID` is provisioned. The companion
   read-only evidence says that gate currently fails closed; this report does
   not override that safety condition.

This report is the bounded baseline audit and canonical source ledger. It is
not an admin override, a production write, or a source-repair implementation;
the PhD/MPhil source integration is a separately proposed change.