---
name: Isolated worker acceptance
description: Schema fidelity and event-loop boundaries for real browser plus worker acceptance tests.
---

Use migration-defined schema semantics when provisioning disposable databases for real worker acceptance, not ORM metadata alone.

**Why:** Raw SQL audit writes rely on server timestamp defaults absent from ORM-created tables. A metadata-only database can produce test-only submission failures before dispatch, despite otherwise valid production behavior.

**How to apply:** Apply relevant real migrations for raw-SQL-owned tables and worker claim infrastructure. Keep the database and broker disposable, and verify process, port, and data-directory cleanup even when an assertion fails.

Database and broker isolation alone do not isolate a real scrape: generated university recipes also need private writable directories.

**Why:** First-scrape configuration generation can write YAML into the checkout even when every service uses disposable infrastructure.

**How to apply:** Redirect all recipe writers to the test's private directory before importing worker services; retain production defaults read-only. Assert the real recipe tree is unchanged and private generated files are removed.

Run asynchronous database snapshots outside the synchronous Playwright caller's thread.

**Why:** Playwright's synchronous API maintains a running event loop; an adjacent `asyncio.run()` raises instead of observing the worker. Keeping the connection and loop in a separate thread avoids cross-loop connection reuse.

**How to apply:** Use a thread-local async snapshot or a synchronous database driver; do not share async connections across loops.

Verify recovery invariants only after the actual Celery task returns, using a fresh database session and restarted API.

**Why:** A terminal job row is not proof that the worker lifecycle has finished: post-terminal recovery work may still mutate data. Logs alone also cannot prove reload persistence.

**How to apply:** Wait for an exact-job task completion signal before taking authoritative snapshots; compare diagnostics and protected records from that snapshot with fresh API projections.