---
name: QMUL duration/degree-level extraction lessons
description: Two general extractor bugs found via QMUL debugging — unused anti-context guards, and fused (non-"doctor") clinical doctorate abbreviations.
---

**Computed-but-unapplied guard variables are a recurring bug class.** `duration.py` computed `anti_duration_context` but never referenced it in the Pattern-0 branch's `if` condition, so UK admissions country-equivalency boilerplate ("Bachelor Degree minimum 5 years... for entry to our postgraduate programmes") matched as if it were the course's own duration.

**Why:** boilerplate that reads like real course-length prose sits right next to real per-course values on the same page; only an anti-context check distinguishes them, and if that check is computed without being wired into the return condition, it silently does nothing.

**How to apply:** when adding an anti-context / suppression variable to any extractor, grep for every `if`/`return` branch that should honor it — don't assume computing the value means it's active. This class of bug (dead guard variable) is worth checking whenever an extractor "sometimes ignores" a documented exception case.

---

**Doctorate name-pattern regexes miss fused UK clinical/professional abbreviations.** `degree_level.py`'s doctorate regex requires the literal substring "doctor" (or phd/dphil/edd/dba). Abbreviations like DClinDent (Doctor of Clinical Dentistry), DClinPsy, EngD, DProf, DrPH, PsyD don't contain "doctor" as a substring at all — they're fused initialisms — so degree_level silently stayed blank.

**Why:** a blank degree_level cascades into wrong fee-sanity-range selection (data_quality.py's `_ANNUAL_FEE_RANGES` keys off degree_level_raw substrings), producing false critical fee-too-high/low flags on legitimate clinical-doctorate fees. This is a general risk for any qualification-name-driven downstream classification (fee range, English requirement tier, etc.).

**How to apply:** when a course/degree-level classifier misses a real qualification, check for fused abbreviations (no natural word boundary around the semantic root word) before assuming it's a one-off site quirk — the fix belongs in the general extractor, not a per-university YAML override.
