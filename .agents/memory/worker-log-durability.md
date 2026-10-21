---
name: Worker terminal log durability
description: A successful worker result does not prove the terminal evidence survived asynchronous log cleanup.
---

Retain buffered log rows until their database transaction commits, and explicitly flush the terminal DONE event.

**Why:** A production release sample completed and returned success, but had no durable DONE record. Cancellation during a background flush can discard a dequeued batch without logging an ordinary exception, causing the release gate to reject an otherwise finished sample.

**How to apply:** Test cancellation during the database write, not only final-drain calls. Preserve failed release evidence; worker success and job counters cannot substitute for the original missing completion payload.
