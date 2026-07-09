---
name: Distributed (Redis) semaphore lessons
description: Nesting order vs local semaphores, per-event-loop client caching, fail-open design for fleet-wide concurrency caps
---

# Distributed semaphore lessons

**Nesting order:** When combining a local in-process semaphore with a
fleet-wide distributed (Redis) slot, acquire the LOCAL semaphore FIRST and the
distributed slot INSIDE it. Otherwise a coroutine holds a scarce fleet-wide
slot while merely queueing behind its own process's semaphore, wasting account
capacity under local contention. (Architect caught this in review.)

**Per-loop Redis client:** `redis.asyncio` connections are bound to the event
loop that created them. Celery prefork tasks may each run their own
`asyncio.run()` loop, so a module-global client breaks when a second loop
reuses a pooled connection. Cache one client per loop in a
`weakref.WeakKeyDictionary` keyed by `asyncio.get_running_loop()` — dead loops
drop their client automatically. Never create a fresh client per acquire —
that's 2 connection setups per HTTP attempt.

**Fail-open pattern for scrape-fleet caps:** disabled by default (cap ≤ 0),
any Redis error logs + proceeds, bounded wait budget (then proceed), and slots
as a sorted-set scored by acquire time so a crashed holder is reaped by the
next acquirer's Lua script once older than TTL (TTL must exceed the longest
single HTTP attempt with headroom).
