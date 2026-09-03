---
name: Vitest accessibility hook ordering
description: Ensures global post-test DOM assertions inspect rendered content before local cleanup removes it.
---

Global DOM assertions registered in a Vitest setup file must use explicit `sequence.hooks: "list"` ordering when test modules also register `afterEach(cleanup)`.

**Why:** With the default stack ordering, module-local cleanup can run first and erase the DOM, allowing accessibility regressions to pass because the global assertion sees no elements.

**How to apply:** For any setup-file guard that inspects rendered DOM after a test, configure list hook sequencing and keep a subprocess integration fixture that proves a deliberately invalid render fails the configured test run.