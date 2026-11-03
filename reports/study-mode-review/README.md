# Development study-mode review

Read-only inventory and official-page review, 3 November 2026 (UTC).

## Results
- All **11,965** development `courses` records inventoried, including every distinct non-null study-mode value (not just values over a length threshold).
- **64 confirmed invalid values**, all at De Montfort University: **57 supported replacement proposals**, **7 unknown replacements**.
- **13 ambiguous labels**: 11 `Both`, 2 `Mixed`. Not confirmed invalid and not proposed for correction.
- **11,286 legitimate delivery/load labels by field meaning**; **602 nulls**, no empty strings. This is semantic triage, not independent re-verification of every otherwise valid record.
- Supplementary inventory of all **36,221 staged rows**: **67 invalid**, 13 ambiguous, 34,009 legitimate labels, 2,132 nulls; no empty strings. Invalid staging rows: 62 approved historical copies, 5 rejected. All 67 included separately, including three additional stored URLs, each inspected.

## Boundaries and interpretation
No course data, staging data, approvals, eligibility, application code or production records changed. Only development SELECT queries and public official-page requests were used. No re-scrapes, repairs, imports or approval endpoints invoked.

`Full-time` / `Part-time` are legitimate study-load labels in this existing field, not delivery proof. Explicit `On Campus, Online`, `Online, On Campus`, `Full-time, Part-time`, and `Blended` values are not invalid merely because they contain alternatives. `Both`/`Mixed` may represent real alternatives but do not identify them; preserved as ambiguous. Nulls are missing, not confirmed corrupt values.

Proposals reproduce labelled course Study mode facts, explicitly labelled course Duration/load facts, or course-owned delivery/attendance sections. Generic scholarships, global opportunities, open days, facilities, online events, course titles and navigation are not delivery evidence. Course-owned teaching delivery was inspected but not used to guess On Campus from lectures/laboratories alone. Unknown delivery remains null even when Full-time study load is verified.

For unknown replacement proposals, **no executable replacement is proposed**. If the user later approves removing invalid prose, blank/null may represent unknown, but this review does not authorize clearing it. The original values remain in the database and in the exports.

Historical snapshots/backups are not current course records and are not cleanup targets. Staging copies are context only; their existing approval/rejection status must not change.

## Important cases
- **14671 Accounting Top Up BSc (Hons)**: labelled Study mode **Full-time** (also repeated in full Key facts). Proposed study_mode `Full-time`; delivery remains unknown in this proposal. No inference from generic campus or online training text.
- **14540 Clinical Pharmacy**: `Part-time, Distance learning (limited campus attendance)`; preserve optional/recommended campus days and module-dependent delivery, rather than flattening to exclusively Online.
- **14529 Cricket Management**: labelled `Part-time, Distance learning`, with course-specific online lectures.
- **14470, 14472, 14531, 14574**: stored URLs HTTP 404; replacement unknown.
- **14484, 14518**: duration alone does not state load; full-time fee wording insufficient to establish complete mode options; replacement unknown.
- **14555**: former Retail Design top-up URL redirects to full three-year degree; do not transfer its delivery proof to an unverified award variant. Same caution for staged **142694** (Heritage Interior Design top-up).
- **14521, 14541**: UK panels advertise part-time alternatives; international panels support full-time only. Proposed combined field label is not international eligibility approval.
- Additional rejected staged **142842**: historical Nursing Associates CPD page supports Part-time, but must stay rejected; no international/degree reinstatement proposed.

## Review files
- `course-review.csv` / `.json`: every affected published-course ID, full original value, exact stored URL, checked time, evidence excerpt, proposed value, unknown delivery/load fields and caveats.
- `staging-review.csv` / `.json`: all 67 staged copies, with table-specific IDs and course links. Do not confuse staged IDs with course IDs.
- `ambiguous-labels.json`: all 13 ambiguous current-course records; preserve, no correction proposed.
- `distinct-values.csv`, `staged-distinct-values.csv`: complete distinct-value inventory, including legitimate labels.
- `development-invalid-inventory.json`, `staged-invalid-inventory.json`: original read-only record snapshots.
- `evidence/*.json`: page titles, retrieval times and course-page excerpts. Keyword matches are retained as review context, **not automatically accepted evidence**; the reviewed quote and decision are in the review files.
- `transport-checks.json`: independent HTTP status/redirect checks for unavailable and partner URLs. The content-fetch service reports the requested URL even after redirect, so use the transport checks for final destinations.
- `inventory.sql`: read-only inventory queries; no mutation or correction SQL supplied.
- `validation.json`: export consistency and unchanged-record comparison.

## Approval needed before any correction
Review the exact table/ID/current-value/proposed-value pairs and caveats. A later approved change must re-read and compare current values, updated timestamps and course identity against this snapshot, reject drift, preserve evidence and approvals, and remain development-only unless production changes are separately authorized. Do not bulk-normalize legitimate labels or replace unknowns with a campus default.
