---
name: Disposable worker reconnect testing
description: Targeting worker sockets without disconnecting the observing test process.
---

Name the worker's and observer's Redis connections separately before injecting
a worker transport disconnect; target only the worker-owned blocking connections.

**Why:** A private broker still contains observer reply sockets. Killing every
blocking connection also breaks the observer, which tests a different failure
and can hide whether the worker actually reconnected.

**How to apply:** For disposable Celery reconnect tests, verify a new worker-owned
connection appears while the original worker process remains alive. Keep queue
inspection and sentinel execution as separate assertions, and verify process
and broker teardown even when the recovery action intentionally fails.
