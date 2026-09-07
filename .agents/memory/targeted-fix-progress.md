---
name: Targeted Fix progress
description: Success semantics and durable-job identity for Review Fix operations.
---

A Review Fix counts as progress only when it updates one of the issue fields shown in that Fix preview. Evidence refreshes and unrelated metadata changes do not resolve a missing target field.

**Why:** OpenAI changed category and entry metadata while fee and duration stayed blank, yet the operation was labelled successful.

**How to apply:** Persist target fields with each durable Fix job, filter reported updates to them, classify target-only evidence refresh as no progress, and never label an all-no-progress result successful.

Durable Fix job identity includes the selected course IDs, target-field set, and source review job. Retries must search all active jobs for an exact match.

**Why:** Course-ID-only matching can attach a duration request to a fee job; checking only the newest active job can miss an older exact match and duplicate work.

**How to apply:** Reuse only a semantically identical queued/running job, regardless of newer nonmatching jobs for the same university.