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

Keep cleanup ownership narrow: database-only maintenance loops must not close
the browser or fetch clients; fetch-capable task boundaries may close only their
current-loop caches after service/session scopes exit. Direct pre-claim recovery
owns a separate connection and must stay independent of the shared pool.

**Why:** Cleanup must preserve task results and ownership fences, rather than
letting a teardown error replace a successful result or the original failure.

**How to apply:** Bound and isolate individual close attempts. Verify repeated
loops with a test-owned engine, read-only queries, isolated network services,
garbage collection disabled, and old loops retained; otherwise weak references
and garbage collection can conceal socket leaks.

Parent-process startup checks and process-death observers must close only their
dedicated engines, never the process-wide application engine.

**Why:** Death observers can run concurrently in parent threads while worker
startup checks use separate loops. Disposing a shared engine from these paths
can interfere with another operation and undermine durable death evidence.

**How to apply:** Give each observer/check a local engine and a bounded awaited
close. Keep shared-pool invalidation confined to serial synchronous task
boundaries, including each separate legacy failure-audit loop.