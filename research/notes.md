# Research Notes: Independent course benchmark

**Status:** collection and excerpt cross-check complete; benchmark approval incomplete
**Depth:** Standard

## Plan
- **Question:** Which official-source facts can independently verify 50 courses across Australia, UK, USA and Canada?
- **Scope:** Course identity, international fees and basis/year/currency, duration, intake, eligibility and English requirements. Search snippets are leads, not verified field truth.
- **Audience:** Portal maintainers.
- **Deliverable:** Source-backed benchmark candidates and explicit gaps; no claims of passing accuracy before agent observations exist.

## Focus Areas
| # | Area | Status | Sources |
|---|---|---|---|
| 1 | Australia, 13 course candidates | collected; gaps retained | 5 |
| 2 | UK, 13 course candidates | collected; gaps retained | 5 |
| 3 | USA, 12 course candidates | collected; gaps retained | 5 |
| 4 | Canada, 12 course candidates | collected; gaps retained | 5 |
| 5 | Tricky eligibility, credit, season and conflicting-source cases | 2 supplemental candidates | 6 |

## Coverage Checklist
- [ ] At least 50 distinct courses have independently checked official evidence.
- [x] All four required countries have research candidates.
- [x] All 179 claimed field excerpts match saved sources after whitespace normalization.
- [ ] Fee year, currency, audience and billing basis are kept distinct.
- [ ] Domestic-only, credit-based and seasonal cases are represented.
- [ ] Genuine conflicting evidence is retained without inventing a conflict.
- [ ] Per-field accuracy is computed from agent outputs, not copied truth.

## Findings Log
Registry: `research/sources.json`. Check results: `research/candidate-evidence-check.json`.

- 52 candidates collected; **not an approved golden set**.
- Western Sydney tables provide explicit annual international fee, duration, campus, intake and CRICOS context. [@source-001]
- Waterloo evidence is Spring 2026 tuition, not proof of Spring admissions or current Fall pricing. [@source-007]
- Excelsia Christian Leadership explicitly combines domestic-only eligibility with English scores; those are not contradictory. [@source-012]
- LSE tables give overseas and home prices separately, with one-year billing context. [@source-018]
- Northeastern rates are per credit, not total programme fees; international-specific applicability remains unverified. [@source-023]
- Initial excerpt checking rejected stitched table-header/row quotations. The corrected dataset now retains contiguous excerpts and separate header context.

## Conflicts & Open Questions
- Official institutional rules must not be misrepresented as course-specific evidence.
- Research candidates are not yet approved benchmark truth.
- No genuine same-course IELTS conflict was established. None was invented.
- Many claims have only one official source, and most candidates lack course-specific English/duration/intake verification.

## Gaps
- Independent observations and complete scenario-suite mapping remain outstanding.
- No per-field extraction accuracy has been measured. Exact-quote matching is not accuracy.
- Historical/cohort-specific prices must not be presented as the latest applicable fee without additional checking.
