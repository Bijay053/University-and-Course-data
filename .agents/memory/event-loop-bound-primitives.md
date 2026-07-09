---
name: Event-loop-bound asyncio primitives in Celery prefork
description: Module-level asyncio.Semaphore/Lock crash with "bound to a different event loop" when Celery tasks each create a fresh loop; use per-loop lazy factories.
---

## Rule
Never create `asyncio.Semaphore`, `asyncio.Lock`, or `asyncio.Event` at module
import time in code that runs inside Celery prefork workers. Each task
invocation may run `asyncio.run()` on a fresh loop, and a primitive created
under an earlier loop raises `RuntimeError: ... is bound to a different event
loop` on first await.

## Why
JCU scrape jobs crashed mid-extraction with loop-binding errors after worker
recycling: module-level semaphores in the fetch layer and stealth-browser
module were created under the first task's loop and reused by later tasks.

## How to apply
- Pattern: a `dict[loop_id → primitive]` (or `WeakKeyDictionary` keyed on loop)
  populated lazily by a getter, e.g. `_get_sem()` looks up
  `asyncio.get_running_loop()` and creates the primitive on first use per loop.
- Applied in `http_fetcher.py` (`_get_sem`/`_get_scrape_do_sem` via `_loop_sem`)
  and `stealth_browser.py` (`_xvfb_lock()`/`_stealth_sem()`).
- `browser_pool` was already loop-safe via its `_ensure()` pattern.
- Tests asserting singleton semantics must be rewritten to per-loop semantics
  (same object within a loop, different objects across loops).
