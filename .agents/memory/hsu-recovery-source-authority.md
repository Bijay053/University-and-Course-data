---
name: HSU recovery source authority
description: Why a null HSU extractor result does not establish unpublished tuition.
---

HSU publishes international tuition using several layouts: inline audience
labels, nested headings, and full-course MSc blocks as well as separate
International headings. Inspect the official source before treating a null
extractor result as an unpublished price.

**Why:** Live recovery found published prices that the narrow heading-based
authority parser returned as null, including full-time PhD and full-course MSc
amounts. Blindly applying the payload would have removed valid tuition.

**How to apply:** Compare the raw official page with the extracted tuple.
Keep full-time separate from part-time and full-course totals separate from
per-credit amounts. Never infer fee year/period from a historical staged value.
If the current page lacks a Location fact, preserve the existing value with an
explicit review warning until a new authoritative source supports correction.

HSU's Master of Chiropractic admissions accordion calls its IELTS requirement
“Undergraduate standard” without naming IELTS, while its PhD page misspells
the acronym “ILETS”. Treat the course-owned admissions context as evidence,
not a page-wide numeric match.

**Why:** Overall scores survived extraction while explicit component floors
were lost; the course pages actually supplied both.

**How to apply:** Preserve each course's own overall/component pair and selected
citations. Do not copy a universal band floor to pages that only link to central
English guidance or publish an overall score alone.

Treat fee currency aliases and selected evidence as part of the authoritative
fee tuple, not only the persisted amount and canonical currency.

**Why:** Adversarial AI testing found a correct GBP database value beside
selected AUD evidence. Updating the payload alone did not clear the earlier
selected marker, and the legacy currency alias could still hold the AI value.

**How to apply:** Reset superseded tuple evidence and reconcile aliases before
final selection. Assert selected metadata as well as amounts after real
staging; a correct snapshot of column values alone does not prove citation parity.

HSU's central English guidance distinguishes undergraduate requirements from
two postgraduate alternatives and explicitly excepts Global Healthcare
Management. Master of Osteopathy is classified as undergraduate despite its
award name.

**Why:** Choosing a level from the word “Master”, or choosing the central page's
first postgraduate score, can assign the wrong requirement.

**How to apply:** Verify the exact course/award and admissions referral; use an
explicit course-owned overall to disambiguate postgraduate alternatives. Fetch
the applicable guidance rather than treating previously observed scores as defaults.
