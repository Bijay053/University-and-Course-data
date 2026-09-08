---
name: Expected-failure teardown proof
description: How destructive integration drills must prove cleanup when the primary operation is intentionally failed.
---

An expected-failure integration drill must reject cleanup warnings and independently query the provider to prove every disposable resource is absent.

**Why:** The deliberately injected error already makes the process exit nonzero, so a test that only expects failure can pass even when teardown also fails and leaves billable resources behind.

**How to apply:** Give the external harness a non-secret run identity and resource metadata, then verify stack/scheduler absence, paginate exact run-tag searches, and check managed-resource identifiers directly after the subprocess exits.