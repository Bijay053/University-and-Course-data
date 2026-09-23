---
name: pytest asyncio isolation
description: Keep pytest async resources on the configured session loop and isolate synchronous helpers
---

## The rule
Keep fully async tests and fixtures on pytest-asyncio's configured session loop, especially when they share asyncpg engines, Redis clients, or other loop-bound pools. When synchronous tests need to call isolated async functions, never rely on ambient `asyncio.get_event_loop()` state; use `asyncio.run()`:

```python
def _run(coro):
    return asyncio.run(coro)
```

## Why
pytest-asyncio with session-scoped test and fixture loops lets shared async pools remain bound to one loop. Creating private loops inside async tests or fixtures makes pooled connections cross loop boundaries; using ambient loop state in sync helpers can instead return a running session loop or a loop closed by an earlier test. Both patterns create order-dependent failures:
- `RuntimeError: This event loop is already running`
- `DeprecationWarning: There is no current event loop`
- Intermittent failures that pass in isolation but fail in the full suite

## How to apply
- Use `asyncio.run()` only in synchronous helpers whose coroutine does not touch session-loop-bound resources
- Fully async tests should await directly and explicitly use the configured pytest-asyncio loop scope
- Async fixtures should use `pytest_asyncio.fixture` and the same loop scope as their tests
- Never create a private loop around shared asyncpg engines, Redis clients, or other pooled async resources
- The symptom of the bug: tests pass in isolation (`pytest tests/test_foo.py`) but fail in the full suite

## Tests that must tolerate multiple loop scopes
Use a test-owned engine with `NullPool` when a database-backed suite must run
under both session and function loops. Close each session in its owning loop;
do not rebind the application's session factory or disable production pooling.

**Why:** Session-loop defaults can mask a shared asyncpg pool's cross-loop
failure. A passing default run alone does not demonstrate loop independence.

**How to apply:** Check the whole file with both the normal configuration and
`-o asyncio_default_test_loop_scope=function`. Keep SQL event listeners bound
to the same test engine used by the test session factory.
