---
name: Testing and verification lessons
description: Pointers for test isolation, lifecycle proof, frontend timing and durable evidence.
---

- [Vitest accessibility ordering](vitest-accessibility-hook-ordering.md) — global DOM checks must precede test-local cleanup.
- [Failure and termination teardown proof](expected-failure-teardown-proof.md) — prove cleanup independently of exit codes and detect rolled-back write attempts.
- [Revision provenance](verification-revision-provenance.md) — evidence certifies its recorded revision, not a later merged tree.
- [Generated API verification](generated-api-verification.md) — generator success does not prove barrel exports or repository-wide types are valid.
- [Injected resource ownership](injected-release-resource-ownership.md) — separate borrowed fixtures from production-owned temporaries.
- [Asyncio isolation](pytest-asyncio-isolation.md) — private schemas, test-owned connections and correct event-loop lifetime.
- [Regression Redis ownership](full-regression-redis-ownership.md) — overlapping suites must not shut down a shared test broker.
- [Database latency validation](database-latency-validation.md) — isolate competing load before changing latency budgets.
- [Deferred React responses](deferred-react-test-responses.md) — resolve controlled responses outside async act when other requests remain pending.
- [Dispatch failure sentinels](dispatch-test-failure-sentinels.md) — broad delivery exception handling can swallow assertion failures.
- [Disposable worker reconnects](disposable-worker-reconnect-testing.md) — distinguish worker and observer sockets before disconnect injection.
- [Evidence proof persistence](evidence-proof-persistence.md) — verify proof after real persistence, not from truncated display excerpts.
- [Environment fixture undo](environment-fixture-undo.md) — deleting an absent environment key does not register teardown for later direct writes.
