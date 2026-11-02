# HSU international tuition and IELTS recovery

## Release

Final production revision: `6e9991a099e08e243cd4b733eacf491e636aec40`.
Completed 2026-11-02 at 06:26:45 UTC; independently verified at 06:27:30 UTC.

The release was restricted to the HSU authority extractor and its regression
tests. It followed an initial guarded release of the inline-price/component
fix, then a second guarded release adding the remaining verified prose-year
and international-only fee layouts. No migration or package update was needed.

Both ordinary pre/post-release samples passed. API and worker identity,
public health, frontend release marker, consumer restoration, idle checks,
and operator-settings preservation passed.

- Final release SSM: `ba312b83-c2a4-4b18-ae0b-d97b9160a52e`
- Final verification SSM: `da8c05d8-7dc0-4c2d-8c2d-20db1f98516c`
- Manifest: `/var/lib/university-portal/release-manifests/release_7e00b3467df942129a2cdd734453b9dc/manifest.json`
- Frontend asset: `/assets/index-BOzFQHZp.js`

## Source and parser changes

The user's Master of Chiropractic example was confirmed from its official
HTML: £17,684 annual international tuition for 2026/27; IELTS 6.0 overall
with 5.5 in each component.

Supported additional layouts include inline domestic/international paragraphs,
nested tuition headings, PhD full-time annual prose, full-course MSc totals,
undated MBA tuition, and the international-only Global Healthcare Management
headline price without substituting its bursary or discounted amount.
Course-owned English accordions now preserve explicit component floors,
including HSU's “Undergraduate standard” and misspelled “ILETS” wording.

Existing same-day official HTML retained by the earlier HSU recovery was used
to check all 31 review identities. Direct live fetching became rate-limited;
this was not a fresh full-catalogue crawl.

## Existing review correction

Review job: `job_004c6eb34596`, university 101, rows 54279–54309.
The operation required this exact 31-row pending set and locked the rows.
It updated only independently populated fee tuples and IELTS values; no
missing parser result was used to clear a fee, and campuses were not changed.

- 15 courses received corrected international fees.
- 3 courses received corrected IELTS values/component floors.
- 27 rows had applicable evidence and durable snapshots refreshed.
- Master of Chiropractic row 54283 was verified at £17,684 annual / 2026,
  IELTS 6.0 overall, and 5.5 listening, reading, writing, speaking.
- All rows remained pending; no approval or publishing occurred.
- A separate read-only check verified selected official evidence and proved
  unrelated row fields were unchanged.
- Persisted row snapshots were reloaded in a new database session and matched.

Backup: `/var/lib/university-portal/hsu-final-release-20261102/review-before.json`

- Recovery SSM: `04fb7d1e-accd-4c01-8587-48b758221bc5`
- Independent data verification: `02561c13-80e8-48a4-a22a-031ffabf2a2b`

## Verification and remaining limits

199 focused release/HSU checks passed on the initial correction. The final
HSU suite passed all 23 tests, including database-backed authority tests and
the added fee/component regressions. The local sign-in page rendered.
The signed-in review UI was not exercised.

This does not prove every HSU English requirement is complete: courses that
only link to central English guidance still need source-specific mapping.
Explicitly unconfirmed overseas tuition and apprenticeship funding bands
remain unfilled rather than being misrepresented as international tuition.
