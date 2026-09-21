---
name: Claimed-worker recovery fencing
description: Safety boundary for autonomously reclaiming repair and verification workers.
---

Never clear or replace a claimed repair or verification worker solely because its runtime or heartbeat is old. Safe autonomous reclaim requires a durable claim generation/token that every later audit, staging, status, and completion write verifies.

**Why:** A delayed worker can remain alive after its heartbeat appears stale. Resetting its job lets a second worker run while the first can still commit, causing duplicate or conflicting course writes.

**How to apply:** Automatic recovery may safely handle pre-claim collisions and unclaimed broker delivery loss. Keep claimed workers fenced until authoritative termination or stop acknowledgement exists; only then revoke their generation and redispatch.