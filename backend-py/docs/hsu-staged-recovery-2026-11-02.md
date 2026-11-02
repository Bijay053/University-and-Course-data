# HSU staged recovery — 2026-11-02

## Authorization and release

The user explicitly approved production staged recovery after release.
Production was verified at `b1b702d95fabe008905a3c0e3a4fddc22d5421a2`.
The deployed `hsu_facts.py` and `single_course.py` hashes matched the corrected
workspace versions. Both services were active. This operation did not deploy
code, approve courses, or publish courses.

## Scope and backups

Production university ID 101; review job `job_a53afb19b2c8`; 31 pending rows,
IDs 54009–54039. Matching used the existing row ID and canonical course URL,
not a fuzzy course-name comparison.

Root-readable recovery evidence is retained on the production host at:

`/var/lib/university-portal/hsu-staged-recovery-20261102/`

- `before.json`: complete original staged rows and field evidence.
- `<row-id>.html`: complete freshly retrieved official source pages.
- `proposals.json`: extractor results with SHA-256 source hashes.
- `reviewed-proposals.json`: individually source-checked recovery values.
- `after.json`: complete persisted staged rows after recovery.

The original backup is exclusive-create and was never overwritten. Production
row locks and an exact comparison against the backup fenced the transaction.
Evidence and post-staging snapshots were updated in the same transaction.
These operational files are not deployed application code.

## Extraction and source review

Direct requests were blocked with HTTP 403; the existing rendered scraping
transport retrieved the official pages. The deployed single-course pipeline
was compared with the HSU authority extractor.

Source review caught additional layouts not handled by that extractor:
inline international amounts, nested fee sections, and full-course MSc fees.
Those values were verified individually against the saved official pages
before writing. A null extractor result was **not** assumed to mean that the
university had not published an international price.

Examples:

- MSc Chiropractic (Pre-registration): £17,684 annual, 2026/27, Bournemouth,
  replacing the £9,790 domestic amount.
- PhD: £17,046 full-time annual, 2026/27, not the £9,143 part-time price.
- Both sport rehabilitation MSc routes: £17,520 **Full Course**, 2026/27,
  not an annual price or a per-credit amount.
- Three MBA routes: £15,500 international tuition, London. Year and period
  remain blank because the course pages did not establish them.
- Four 2027 undergraduate routes explicitly say overseas fees remain
  unconfirmed; their domestic £10,050 amounts were cleared.
- Apprenticeship funding bands were not retained as international tuition.

## Persisted outcome

All 31 rows remain `pending`, with `auto_publish_status=review`.

| Field | Changed rows |
|---|---:|
| Course location | 30 |
| International fee | 21 |
| Currency | 3 |
| Fee year | 14 |
| Fee term | 11 |

Eight international fees remain blank. Every row now has GBP currency.
There were **zero AUD rows before recovery**, so no AUD removal is claimed.

One campus remains unresolved: MSc Sport Rehabilitation and Therapy
(Pre-registration), row 54026. Its current official page provides no
course-owned Location fact. Its previous campus value was preserved and
explicitly flagged `hsu_location_unverified`; its independently verified
£17,520 full-course fee was corrected.

The FdSc Assistant Practitioner – Podiatry page explicitly identifies
**Online Delivery**. This replaces the fabricated campus list and carries a
review warning. It was not published.

## Verification

A separate read-only production check verified:

- All 31 stable identities and pending/manual-review statuses.
- Every intended value against the reviewed source proposals.
- No changes to unrelated course fields.
- Selected persisted official citations for nonblank corrected fields.
- Removal of obsolete fee evidence where international tuition is blank.
- Exact agreement between every final row and its durable staged snapshot.
- Published HSU inventory remained empty before and after recovery.

Verification command receipt: `3071ebf7-43c2-4ba9-b7e7-f4ce5cc5e032`.
The signed-in review UI was not exercised; verification was against persisted
production rows, evidence, and snapshots.

## Remaining source limitations

Do not infer a missing campus or MBA fee period/year. These require new
official evidence and manual review. Normal HSU extraction still needs
coverage for the additional fee layouts identified above; this recovery
used individually verified source evidence rather than deploying a new parser.
