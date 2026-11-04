---
name: Expected-failure teardown proof
description: How destructive integration drills must prove cleanup when the primary operation is intentionally failed.
---

An expected-failure integration drill must reject cleanup warnings and independently query the provider to prove every disposable resource is absent.

**Why:** The deliberately injected error already makes the process exit nonzero, so a test that only expects failure can pass even when teardown also fails and leaves billable resources behind.

**How to apply:** Give the external harness a non-secret run identity and resource metadata, then verify stack/scheduler absence, paginate exact run-tag searches, and check managed-resource identifiers directly after the subprocess exits.

For graceful ASGI termination, prove shutdown by completed lifespan cleanup and
the durable claim transition, not by assuming exit status zero.

**Why:** Uvicorn can restore and re-raise SIGTERM after successful cleanup,
producing a negative signal exit code even though cancellation and requeue worked.

**How to apply:** Require a shutdown marker, cleared running activity and a
requeued claim; independently verify child processes and listening sockets are
gone. Accepting SIGTERM alone would also accept a process that skipped cleanup.

A read-only test should detect attempted writes even when the transaction rolls
back or the application catches the exception.

**Why:** Unchanged final rows prove no lasting mutation, not that research avoided
calling a writer.

**How to apply:** In an owned disposable database, a rejecting trigger can advance
a PostgreSQL sequence before raising. Sequence advancement survives rollback,
providing an independent sentinel for otherwise swallowed write attempts.