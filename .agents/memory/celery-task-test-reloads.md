---
name: Celery task test reloads
description: Prevent registered task objects from bypassing safety stubs after module reload.
---

Task-boundary tests must stub the registered task function's dependencies, not
assume the currently imported module owns that function.

**Why:** Celery keeps registered task objects when an import-smoke test removes
and reimports their module. A stub on the new module can miss the old function's
globals and accidentally execute real snapshot writes during a full test suite.

**How to apply:** Patch the registered function's actual globals or invoke a
test-owned function directly. Include import-reload ordering in maintenance
test verification; a passing standalone file does not prove full-suite safety.