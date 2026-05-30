---
name: pytest asyncio isolation
description: How to write sync test helpers that call async code without conflicting with pytest-asyncio's session-scoped event loop
---

## The rule
When writing synchronous test methods (class-based tests) that need to call `async` functions, never use `asyncio.get_event_loop().run_until_complete(coro)`. Use a fresh loop instead:

```python
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
```

## Why
pytest-asyncio with `mode=Mode.AUTO` and `asyncio_default_fixture_loop_scope=session` creates a session-scoped event loop. When `asyncio.get_event_loop()` is called from a sync test method, it may return the session loop (which is running) or a closed loop, depending on test ordering. This causes:
- `RuntimeError: This event loop is already running`
- `DeprecationWarning: There is no current event loop`
- Intermittent failures that pass in isolation but fail in the full suite

## How to apply
- Always use `asyncio.new_event_loop()` in sync `_run()` helpers
- Tests that are fully async should use `async def test_...()` — pytest-asyncio AUTO mode picks them up automatically without any decorator
- The symptom of the bug: tests pass in isolation (`pytest tests/test_foo.py`) but fail in the full suite
