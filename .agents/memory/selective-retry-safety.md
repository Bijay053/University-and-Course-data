---
name: Selected failure retry safety
description: Conservative attempt accounting and source identity requirements for selective course recovery.
---
Keep retries tied to structured failure provenance, not repeated log wording. Settled checkpoints can represent errors or skips, not successful imports.

**Why:** Repeating successful work or treating a completed worker as verified course recovery previously obscured missing-course failures.

**How to apply:** Preserve the original job and progress. Reserve attempt capacity before extraction; uncertain or interrupted attempts remain spent. Do not release unused reservations merely because a heartbeat is old or a terminal label appeared; require authoritative worker-stop proof. Verify resulting records before reporting recovery.

Freeze a stopped generation before counting unused capacity, and never re-execute a child whose capacity was released. Give restored capacity to a new child in the same original lineage.

**Why:** A delayed broker delivery can have read the queued job before the stop receipt existed. Reusing that child could spend slots already assigned to its successor.

**How to apply:** Serialize release with fenced attempt transactions, retain the immutable release receipt, and recheck durable generation binding after obtaining the execution claim. Legacy unbound executions remain fully reserved.

Reused extraction results must have current source bytes, deterministic selected evidence and matching identity, audience, year, configuration and parser build. Never treat an old pending row as a source-validated cache.

**Why:** Browser states, fee variants and uncommitted parser edits can change facts without changing a course URL.

**How to apply:** Capture loaded worker source identity from content, not filesystem timestamps alone. If proof is incomplete, extract afresh. Backend confirmation flags are policy requirements that every chat action must honor.

Matching HTML is not proof that a transport read the current source. Require
invocation-local live provenance as well; historical archives, replay and
unknown adapters must not validate ordinary URL-retry reuse.

**Why:** The normal transport ladder can successfully return archived HTML.
Treating any successful return as current would let old facts validate old cache entries.

**How to apply:** Preserve the exact winning response before transformations.
On a miss, continue extraction on those bytes rather than downloading again.
Keep auxiliary multi-document responses outside single-source reuse unless
every contributing source can be independently validated.

Recheck source ownership in the actual staging transaction and hold its lock through the course commit.

**Why:** A reservation-time check cannot prevent ownership changing during extraction; testing an unused helper does not protect the real write path.

**How to apply:** Exercise the production staging callsite, including a source change after reservation and a concurrent writer blocked until commit.
