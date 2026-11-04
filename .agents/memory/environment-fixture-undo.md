---
name: Environment fixture undo
description: Prevent configuration-loader tests from leaking direct environment writes into later tests.
---

Fixtures testing configuration loaders must register restoration for keys the loader may create, even when those keys are initially absent.

**Why:** Pytest monkeypatch deletion of an absent key records no undo. A loader's later direct environment update can therefore survive teardown and leave an expired temporary certificate path affecting unrelated HTTP clients and child processes.

**How to apply:** Register the original state before clearing the key, or isolate loader execution in a subprocess. Verify the loader tests followed by real client/resource tests in the same pytest process; testing each module alone misses this contamination.
