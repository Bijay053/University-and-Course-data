# HSU linked English recovery

Official pages inspected on 2026-11-02. No production deployment or pending-row
mutation was performed in the task workspace.

## Source and applicability

Central source:
https://www.hsu.ac.uk/study/apply/how-to-apply/international-applicants/english-language-requirements/

The IELTS Academic table specifies:

- Undergraduate: 6.0 overall with no less than 5.5 in each component.
- Postgraduate: 6.0 overall (5.5 each component) **or** 7.0 overall
  (minimum 6.0 each component), depending on the course.

It explicitly excludes MSc Global Healthcare Management. Its reference to
“the majority MSc courses” is not evidence for assigning a score to an
unidentified postgraduate award. TOEFL rows likewise do not unambiguously map
all courses, so this recovery does not infer TOEFL or PTE values.

The following exact official course paths and H1 awards were inspected under
https://www.hsu.ac.uk/course/:

| Path | Award / admissions evidence | Applicable route |
|---|---|---|
| bsc-hons-physiotherapy | BSc (Hons) Physiotherapy; refers to English Language Requirements guidance | Undergraduate |
| bsc-hons-human-nutrition | BSc (Hons) Human Nutrition; same admissions referral | Undergraduate |
| bsc-occupational-therapy | BSc (Hons) Occupational Therapy; same admissions referral | Undergraduate |
| bsc-hons-speech-and-language-therapy | BSc (Hons) Speech and Language Therapy; same admissions referral | Undergraduate |
| bsc-hons-sport-rehabilitation | BSc (Hons) Sport Rehabilitation; links central English requirements | Undergraduate |
| master-of-osteopathy | Master of Osteopathy; explicitly labelled Undergraduate; links central English requirements | Undergraduate, not inferred from “Master” |
| msc-osteopathy-pre-registration | MSc Osteopathy (pre-registration); international section requires “IELTS level 7.0 or equivalent” | Postgraduate 7.0 route; course owns overall, central table supplies 6.0 components |

Other inspected courses publish their own requirements: podiatry and
radiography 7.0/6.5, medical ultrasound 7.0/6.5, chiropractic MSc 7.0/6.0,
speech-language therapy MSc 8.0/7.5, sport rehabilitation MSc and performance
psychology MSc 6.0/5.5. These are not mapped to a universal requirement.
Apprenticeship and foundation routes are not aliases of the listed awards.

## Runtime safety

Recovery requires both the exact official course path and matching H1, plus
an admissions-owned English referral or IELTS statement. A navigation link
alone cannot trigger it. The central IELTS table is fetched with a bounded
timeout and parsed at runtime; no stored numeric default stands in for a failed
fetch. Unknown or conflicting routes are not completed. Course-owned
overall/component pairs win without fetching central guidance.

Selected evidence retains the central row, exact award/course identity and
source URL. Overall-only postgraduate requirements retain their course citation;
only components cite the central source. Equal-valued generic candidates are
superseded to prevent a guessed citation winning selection.

## Verification and release boundary

Tests exercise the deterministic pipeline, fail-closed cases, and a private
socket-only PostgreSQL cluster through staging and the persisted review endpoint.
Existing HSU tuition tests remain in the regression set. English recovery never
copies apprenticeship funding or unpublished domestic tuition into international
fees.

Before any later production correction: use the existing guarded EC2 release
procedure, capture and retain an exact pending-cohort backup including evidence,
and fence updates on row IDs, job/university ownership, pending status and the
unchanged pre-update payload/evidence. Abort on drift; never widen to all HSU
rows. Re-read persisted review values and selected citations after correction.
This document does not authorize deployment or data writes.
