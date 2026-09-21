---
name: Claimed-worker recovery fencing
description: Safety boundary for autonomously reclaiming repair and verification workers.
---

Never clear or replace a claimed repair or verification worker solely because its runtime or heartbeat is old. Safe autonomous reclaim requires a durable claim generation/token that every later audit, staging, status, and completion write verifies.

**Why:** A delayed worker can remain alive after its heartbeat appears stale. Resetting its job lets a second worker run while the first can still commit, causing duplicate or conflicting course writes.

**How to apply:** Automatic recovery may safely handle pre-claim collisions and unclaimed broker delivery loss. Keep claimed workers fenced until authoritative termination or stop acknowledgement exists; only then revoke their generation and redispatch.

Task failure and process death are different facts. A worker may report a failure while still running, and a timeout notification may arrive before termination.

**Why:** Treating a task-result exception as death can admit a conflicting replacement; waiting for a particular failure callback can miss a real death.

**How to apply:** Require evidence tied to the exact execution process, independently of application-reported failure. If authoritative evidence is unavailable, keep ownership fenced.