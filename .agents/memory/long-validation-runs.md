---
name: Long validation runs
description: Distinguishing large-suite runner limits from actual test failures.
---
Run long checks in tracked background shells, with retained output and a finite check-in, rather than repeatedly restarting them after a foreground timeout.

**Why:** The automatic completion runner exhausted its polling window while a full scraper suite was still progressing. A scoped suite also outlasted the foreground shell limit. Resource pressure has separately caused temporary PostgreSQL initialization and unrelated UI cases to time out. Neither partial progress nor a runner timeout establishes that the tests passed.

**How to apply:** Keep the tested source snapshot stable, wait for an actual terminal exit, and report the exact checks that finished. Do not disable meaningful checks or report a partial suite as full release approval. Stop any owned background task that is no longer needed.

Serialize memory-heavy database fixtures and frontend tests; a single frontend worker reduces contention. A private-database startup failure is an unresolved harness error, not a passed behavioural test. Keep focused checks separate from full release approval, and do not increase application deadlines to compensate for a constrained test host.
