---
name: Discovery alert scope
description: Distinguish deliberately selected course recoveries from low-coverage catalogues.
---

Apply catalogue-size alert thresholds only to full discovery, not to explicitly
selected course URLs. Selected recovery alerts must use the extraction outcome;
a submitted URL is not evidence that fetching or staging succeeded.

**Why:** A successful one-course Northumbria report retry raised a catalogue
discovery failure solely because its intentionally bounded candidate count was
one. Globally suppressing small-run alerts would hide genuinely broken catalogues.

**How to apply:** Reuse the worker's validated URL scope rather than assuming a
report label, parent job, or fast mode means bounded discovery. Preserve failed
selection alerts, and evaluate budget exhaustion only after the bounded wrapper
has finalized its counters. Keep alert decisions separate from progress,
review-only staging, recovery audits, and historical alert records.

Stage persistence failures can be counted as skips rather than errors by legacy
accounting. Do not equate zero error counters with recovery success.

**Why:** A failed commit can leave no staged review row while the job still
reports completed. Suppressing an alert based only on status and errors hides it.

**How to apply:** Inspect actual staging outcomes separately from counters;
exclude deliberate eligibility rejection and already-resolved duplicates, and
clear a transient failure when a later attempt succeeds.

Keep scope-aware failure alerts reachable from early guard aborts as well as
normal completion.

**Why:** The domain safety guard can reject a report URL accepted against the
university's website host when its catalogue uses a different host. Its early
return bypassed finalization.

**How to apply:** Preserve the safety guard, but route selected recovery aborts
through persisted failure handling; test the real orchestrator path.
