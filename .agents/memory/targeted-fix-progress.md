---
name: Targeted Fix progress
description: Success semantics and durable-job identity for Review Fix operations.
---

A Review Fix counts as progress only when it updates one of the issue fields shown in that Fix preview or legitimately replaces that target field's selected provenance. Merely requesting or re-saving evidence does not count, and unrelated metadata changes do not resolve a missing target field.

**Why:** OpenAI changed category and entry metadata while fee and duration stayed blank, yet the operation was labelled successful.

**How to apply:** Persist target fields with each durable Fix job, filter reported updates to them, count only detected selected-provenance changes, and never label an all-no-progress result successful.

Target fields also constrain writes, not only progress reporting. A targeted Fix may persist the requested fields and explicit semantic companions, but must discard unrelated extracted values and evidence.

**Why:** A fee-only Fix previously wrote AI-inferred entry requirements and refreshed CRICOS/category evidence even though those fields were not under review.

**How to apply:** Pass targets into re-extraction. For fees allow amount, period, year, and currency; define similarly narrow companion sets for duration, location, English subscores, intakes, and academics.

The results dialog must re-run target-field analysis after a job completes and derive its badge from the before/after missing counts. Generic worker completion or unrelated changed fields are not sufficient evidence of success.

**Why:** Older or resumed jobs can report completed work and metadata updates while every requested gap remains missing.

**How to apply:** Treat an after-analysis field omitted from the issue list as zero missing, show unchanged target counts as “No progress,” and label non-target changes as other metadata.

Durable Fix job identity includes the selected course IDs, target-field set, and source review job. Retries must search all active jobs for an exact match.

**Why:** Course-ID-only matching can attach a duration request to a fee job; checking only the newest active job can miss an older exact match and duplicate work.

**How to apply:** Reuse only a semantically identical queued/running job, regardless of newer nonmatching jobs for the same university.

An extractor correction for an existing nonblank value also needs a matching Fix-analysis issue.

**Why:** Winchester's corrected full title was extracted successfully, but the saved bare subject was not identified as defective, so targeted Fix excluded the name from persistence.

**How to apply:** Test analysis → requested targets → persisted fields together. Detect the defect with bounded source evidence, and ensure it disappears after correction rather than rewriting unrelated fields.

Smart Fix uses actual before/after issue resolution, not legacy value/provenance-change completion, and derives each course's targets from its own unresolved issues.

**Why:** ULaw's bulk run counted many courses as completed through provenance changes while thirteen others repeated the same extraction twice. “Skipped” concealed attempted-but-unchanged results, and completion did not mean the requested gaps were fixed.

**How to apply:** Keep attempted/no-progress separate from not-attempted and already-resolved. Retry only with distinct relevant official evidence within the shared deadline. Preserve first-pass gains; a disappeared row, failed extraction, or failed post-analysis cannot prove resolution. Persist mode and exact target identity so legacy and smart jobs cannot attach to each other.