---
name: IELTS split overall/band phrasing trap
description: When the overall IELTS sits in a banner and the per-band floor in prose, the broad fallback grabs the per-band number as the overall
---

# IELTS extraction: split overall (banner) + per-band (prose) trap

Some university pages state the IELTS **overall** in a heading/banner and the
**per-band floor** in separate body prose. UWL (University of West London) is the
canonical case:

- Banner: `"6.0 IELTS or above"` (overall; number can sit BEFORE the keyword)
- Prose: `"a minimum of IELTS 5.5 for each of the four individual components"`

**Trap:** a broad "first `ielts <digit>` wins" fallback matches the per-band
number (the lower `5.5`) and returns it as the *overall*, dropping the true
`6.0`. Symptom: the IELTS column shows the band floor where the overall belongs.

**Rule:** treat overall and per-band as two independent signals that can live in
different sentences/elements. Only collapse the per-band number onto all four
bands when an **"each component/band/section"** cue is present — that cue is the
tell that a number is a *floor*, not the overall. Resolve this split structure
**before** any broad single-number fallback, and require BOTH signals so a page
that states only one bare score cannot false-positive.

**Why:** when overall and per-band are stated separately the per-band number is
almost always lower and carries the "each ..." cue; without this guard the broad
fallback silently downgrades the overall to the band floor.

**How to apply:** any new "grab the first IELTS score" heuristic in the english
extractor must run after the split-structure check, or it will regress UWL-style
pages. See `test_english_ielts_uwl_split_*` for the locked behavior.
