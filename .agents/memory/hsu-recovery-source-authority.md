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
