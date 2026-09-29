---
name: Deferred React test responses
description: Avoid stalled interaction tests when resolving a controlled request while other React work is pending.
---

For interaction tests that hold a network response open, resolve the deferred promise directly and then use a DOM assertion with `waitFor` to observe the update. Avoid wrapping the resolver in an awaited async `act` when other requests may still be pending.

**Why:** An awaited async `act` remained unsettled even after the targeted response resolved; the DOM assertion completed when the resolver was called directly.

**How to apply:** Use this pattern only for tests with controlled, delayed network responses and confirm the expected rendered state after resolution.