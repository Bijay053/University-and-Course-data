---
name: Celery loop resource cleanup
description: Why loop-owned network resources must be closed before per-task event loops end.
---

Close every loop-owned HTTP client and database pool while the Celery task's
event loop is still running. Garbage collection and next-task pool invalidation
are not substitutes for deterministic closure.

**Why:** Long-lived prefork children accumulated keep-alive sockets across
fresh per-task event loops until they reached the process soft file-descriptor
limit. New jobs were received but failed before being claimed, leaving the UI
misleadingly queued.

**How to apply:** Any new shared client or connection pool scoped by event loop
must have an awaited task-boundary cleanup path. Keep a production descriptor
limit and bounded child recycling as defense in depth, not as the primary fix.