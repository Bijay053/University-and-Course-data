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
