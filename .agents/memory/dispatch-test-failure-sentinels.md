---
name: Dispatch test failure sentinels
description: Make dispatch-order assertions visible despite best-effort exception handling.
---

When testing best-effort API dispatch, assert exact success/failure totals outside
the delivery callback, not only assertions inside mocks.

**Why:** The API deliberately catches delivery exceptions, which also catches an
AssertionError from a test callback. Without external outcome checks, an ordering
regression can appear to be an expected broker failure and falsely pass.

**How to apply:** Record outcomes only after ordering assertions have passed, then
check exact totals against the configured mock delivery scenario after requests.

## Real concurrency isolation

Disposable schemas do not isolate PostgreSQL advisory locks. Concurrency tests
must also keep their lock identities outside the application's university IDs.

**Why:** Otherwise a test that never touches application tables can still block
a real scrape request or contend with another suite through database-wide locks.

**How to apply:** Use test-owned identities in a separate key range and require
PostgreSQL to report a waiting transaction before releasing a test barrier.
Simultaneously scheduled HTTP coroutines alone do not prove lock contention.
