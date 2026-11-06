---
name: Selected failure retry safety
description: Conservative attempt accounting and source identity requirements for selective course recovery.
---
Keep retries tied to structured failure provenance, not repeated log wording. Settled checkpoints can represent errors or skips, not successful imports.

**Why:** Repeating successful work or treating a completed worker as verified course recovery previously obscured missing-course failures.

**How to apply:** Preserve the original job and progress. Reserve attempt capacity before extraction; uncertain or interrupted attempts remain spent. Do not release unused reservations merely because a heartbeat is old or a terminal label appeared; require authoritative worker-stop proof. Verify resulting records before reporting recovery.

Reused extraction results must have current source bytes, deterministic selected evidence and matching identity, audience, year, configuration and parser build. Never treat an old pending row as a source-validated cache.

**Why:** Browser states, fee variants and uncommitted parser edits can change facts without changing a course URL.

**How to apply:** Capture loaded worker source identity from content, not filesystem timestamps alone. If proof is incomplete, extract afresh. Backend confirmation flags are policy requirements that every chat action must honor.

Recheck source ownership in the actual staging transaction and hold its lock through the course commit.

**Why:** A reservation-time check cannot prevent ownership changing during extraction; testing an unused helper does not protect the real write path.

**How to apply:** Exercise the production staging callsite, including a source change after reservation and a concurrent writer blocked until commit.
