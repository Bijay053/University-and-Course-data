# UEL source-template findings

Run: `20260918T022553Z_1910e8` (partial capture inspected while the isolated
run was still active). These findings come from the saved HTML, not from a live
request.

## Two pages rejected by strict option-row validation

The production parser first groups every
`.course-option-details-item-wrapper` under its preceding `.degree-type`, then
requires **every** grouped row to contain both a recognised applicant and
attendance value (`uel_variants.py`, `parse_uel_variants`, validation loop).
Consequently one non-option/status row aborts the whole multi-route page.

### Physiotherapy BSc (Hons)

- URL: `https://www.uel.ac.uk/undergraduate/courses/bsc-hons-physiotherapy`
- Capture: `b94d79380883faa0f20ef064d9937e2686199302f87065cc40fdcad90a7aacc6.html`
- Affected label: `Degree`.
- Source row: `International Applicant, Fees: International applications will
  open later this year`. It has `.application-type = International Applicant`
  but an empty `.attendance-type`; it has no duration or application action.
  This is genuinely blank upstream scheduling data, but the DOM and status
  sentence unambiguously identify a non-actionable international application
  status stub rather than a complete course option.
- Whole-page failure also loses the valid sibling `Degree via foundation year`
  (Home full-time, four years), as well as valid Home full-time rows and the
  route-owned eligibility panel for `Degree`. Route IDs lost are
  `...?uel_variant=degree` and
  `...?uel_variant=degree-via-foundation-year`.

### Psychology BSc (Hons)

- URL: `https://www.uel.ac.uk/undergraduate/courses/bsc-hons-psychology`
- Capture: `236567a1a3c69ef9df5511f968c3fe1e0ecd3bab18fa637558bfdcbcc11403c6.html`
- Affected label: `Degree`.
- Source row: `Distance Learning BSc (Hons) Psychology (Distance Learning)`.
  Both `.application-type` and `.attendance-type` are absent. The row is an
  unambiguous link to another course, embedded with the option-row CSS class;
  it is not an applicant/attendance option.
- Whole-page failure loses both otherwise complete labels, `Degree` and
  `Degree with foundation year`, including their explicitly bound eligibility
  screens. Route IDs lost are `...?uel_variant=degree` and
  `...?uel_variant=degree-with-foundation-year`.

**Safe fix concept (not implemented):** classify rows before strict validation.
Accept an option only when it has the explicit applicant and attendance
contract. Separately recognise (a) a no-attendance application-status stub by
its explicit applicant plus status-only text/no application action, and (b) a
no-applicant/no-attendance linked-course card by its course link. Exclude only
those narrowly proven non-option rows; continue rejecting unexplained partial
rows. For Physiotherapy, preserve the explicit international-applicant evidence
as eligibility metadata, but do not invent attendance, duration, fee, or use
that stub as the scoped extraction row. This retains fail-closed behaviour for
unknown templates.

## Three labels without route-owned eligibility content

These are source omissions, not merely unsupported alternate bindings:

1. `https://www.uel.ac.uk/postgraduate/courses/mres-creative-arts-social-justice`
   — label `MRes`, capture
   `c49af9333ee6f463c7b537563277c548e2d0f3830cb518ca09004a17f5169ae2.html`.
   The page has complete Home/International option rows but no
   `.entry-requirement-wrapper`, no eligibility modal trigger, and no
   route-labelled eligibility content. “Entry requirements” occurs in page
   navigation/application boilerplate only.
2. `https://www.uel.ac.uk/undergraduate/courses/bsc-hons-societies-digital-innovation`
   — label `Degree`, capture
   `359d8e50ebe40d4f70a935ba527e36048d4757c28937c99471f774800c1d8d45.html`.
   It has Home full/part-time options, but likewise no entry-requirement block
   or eligibility binding. The only “entry requirements” occurrence outside
   navigation is generic application advice.
3. `https://www.uel.ac.uk/postgraduate/courses/prof-doc-performing-arts`
   — label `Professional Doctorate with distance learning`, capture
   `22f170d393fe6bfb6462d9a7de7a5ca115503aa82acd45bfaaa40d4af85a92f4.html`.
   The entry block contains a row and label for this route, but no button,
   target, or content beneath it. Its sibling `Professional Doctorate` has the
   normal `Full entry requirements` button bound to
   `2646-inner-loop-1-parent-1`, whose content starts `MA in Performance or
   related discipline...`. That sibling content cannot defensibly be assigned
   to the distance-learning label.

The current parser does not throw for these missing panels:
`_requirements` returns empty and scoped variants are marked
`data-uel-requirements="missing"`. The first two are single-label pages (no UEL
variant expansion); the third still emits both doctorate routes, with only the
standard doctorate owning requirements.

**Safe fix concept (not implemented):** no parser fallback should copy a nearby
or generic panel. Keep explicit “source eligibility missing” evidence. If
staging policy requires eligibility evidence, route these records to a
documented skip/review reason rather than silently applying a default. Only a
future source contract with an explicit label-to-panel relationship should
make them extractable.

## PGCE canonical collision

Two captures canonicalise to
`https://www.uel.ac.uk/postgraduate/courses/pgce-primary-sen-inclusion`:

- `ae25be8ca71d9a38e950b4cc67d43a8a7e9cc8ec65404caae33db05944d297f0.html`
- `d3145524e2110ae97cf860d0461ac71206444222dd08af96e2bda2a79e74758f.html`

Both declare the same canonical and `og:url`, title `Primary with SEN
(Inclusion) PGCE`, `PGCE` option rows, and eligibility target
`2961-inner-loop-1-parent-1`. Normalised visible article text, the requirements
block, and course-options block are byte-identical after DOM parsing. Raw-byte
differences are confined to Akamai/BOOMR request telemetry (for example
different edge host and request ID), not course evidence. They therefore
represent one source/route identity and must not stage twice.

The final manifest confirms the two original discovery inputs:

- `ae25...d297f0`:
  `https://www.uel.ac.uk/postgraduate/courses/pgce-primary-sen-inclusion`
- `d314...4758f`:
  `https://www.uel.ac.uk/postgraduate/courses/pgce-primary-sen-special-schools`

This is a discovery-alias collision, not a repeated fetch. The latter input
canonicalises to the former. Canonical route identity, not discovered URL or
name, must control deduplication.

## Final isolated-run diagnosis

The final independent report is
`.local/uel-audit/20260918T022553Z_1910e8/uel-catalogue-evidence-report.json`.
Discovery logged 276 links (114 UG and 162 PG inputs). The manifest has 275
captures: the 114 UG inputs and 161 PG inputs. After the PGCE alias is
canonicalised, that is 274 captured source pages (114 UG, 160 PG).
`pgcert-autism-spectrum-conditions-learning` was processed through ordinary
browser recovery (`skip_initial_http` and `browser_http_fallback` logs).
The wrapper records only successful UEL preflight fetches, so this recovered
source has no raw capture in the audit manifest. Its independent route count
is therefore not proven beyond the one processed candidate.

The 274 captured canonical pages own 404 routes. A further uncaptured PGCert
source accounts for at least one route, giving 405 canonical catalogue
identities. Route conservation is:

| Source category | Source routes | Staged | Not staged |
|---|---:|---:|---:|
| UG standard | 114 | 76 | 38 |
| UG foundation | 81 | 74 | 7 |
| PG MA | 31 | 14 | 17 |
| PG MFA | 7 | 7 | 0 |
| PG placement | 40 | 39 | 1 |
| PG other standard | 131 | 85 | 46 |
| **Captured total** | **404** | **295** | **109** |

All 295 staged rows match a captured canonical route ID and no route ID is
duplicated in staging. All 109 captured-but-unstaged identities have a runtime
outcome: 27 deliberate exclusions (10 domestic-only, 9 part-time-only, 8
online-only), 78 duration-payload contract failures, and four route identities
lost through the two whole-page template failures described above. The
uncaptured PGCert is another duration-payload failure.

The job's 82 error events comprise:

- 80 `InvalidPayloadKeyError` events because extraction emitted the
  non-persistable keys `duration_unit` and `duration_value`. This is a real
  extraction/staging contract defect, not a source-template or independent
  parser limitation. One event is the PGCE alias whose canonical sibling did
  stage; the other 79 represent unique canonical route losses (including the
  uncaptured PGCert).
- Two strict UEL option-row errors (Physiotherapy and Psychology). Those two
  candidate failures represent four independently evidenced source routes.

IELTS comparison has three representative classes:

1. **Real value error (9 rows):** source panels explicitly say overall 6.0,
   writing/speaking 6.0, listening/reading 5.5, while staging records 6.0 for
   listening/reading. Examples include Applied Theatre MA, Data Science MSc,
   and Education Top Up BA. The staged component precedence/integration is
   wrong.
2. **Production parser limitation (2 rows):** both AI and Data Science MSc
   routes say `IELTS 6.0 (Writing and Speaking 6.0, Listening and Reading
   5.5)`. Overall is staged but all four components are null because the
   production UEL component parser does not support skill-before-score
   parenthetical grammar.
3. **Not source-verified (15 rows):** staging has overall 6.0 where the owned
   panel has no IELTS numeric statement. These are configuration/default
   values (principally PGCE/research pages), not independently sourced course
   values.

An initial comparator warning on `Writing, Speaking;, Listening and Reading`
was a comparator punctuation limitation. The independent walker was corrected
to accept that unambiguous grouped list; it is not present in the final 26
IELTS mismatches. Thus no known comparator false positive remains in the final
classes.

Eligibility propagation has a separate real gap: 49 staged single-route rows
have `international_eligible = null` despite explicit source option audiences
(48 explicitly international-capable; one explicitly Home-only). The Home-only
MRes row is nevertheless marked eligibility `ready/ok`. Multi-route scoped
rows preserve the audience boolean; the gap is in single-route propagation,
not source ambiguity.

Timing evidence: job start `02:26:10.537690Z`, completion
`02:43:57.602121Z`; UEL expansion progress ran from `02:26:21.083085Z` to
`02:29:47.900166Z`. The terminal log reports discovery 7.1s, extraction
1058.2s, and staging 1.2s.

Release/parity evidence identifies revision
`7e0673caa3fb151694a4f22ba66aeb0356c77544` and records SHA-256 values for the
orchestrator, UEL transport, UEL variant parser, and UEL YAML. These match the
production/extracted discovery and extraction inputs checked for this run.
The intentional differences are the isolated runtime/job identity and the
initial seed-root representation versus the UG listing URL; they do not alter
the released discovery/extraction implementation. The process exited 0,
approval remained null, all 295 rows remained pending, and the source review
row count was unchanged.