---
name: Background worker resource lessons
description: Ownership, event-loop cleanup, isolation, and test-reload safety for background workers.
---

- [Celery loop resource cleanup](celery-loop-resource-cleanup.md) — close loop-owned resources before shutdown; parent observers own dedicated engines.
- [Celery task test reloads](celery-task-test-reloads.md) — retained registered task objects can bypass stubs on reloaded modules.
- [Event-loop-bound primitives](event-loop-bound-primitives.md) — avoid cross-loop semaphore and lock reuse.
- [Distributed semaphore lessons](distributed-semaphore-lessons.md) — local slot ordering and loop-owned Redis clients.
- [Pre-claim failure ownership](preclaim-failure-ownership.md) — direct emergency writes remain outside shared pools and transition only queued jobs.
- [Repair pre-claim schema readiness](repair-preclaim-schema.md) — service health alone does not prove delivered tasks can claim work.
- [Isolated worker acceptance](isolated-worker-acceptance.md) — retain migration-defined defaults and separate database checks from synchronous browser loops.