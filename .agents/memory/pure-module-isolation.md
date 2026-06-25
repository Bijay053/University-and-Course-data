---
name: Pure-function extraction for isolated testing under high CPU load
description: When scraper code needs to be testable under Celery-saturated load, extract pure functions into a stdlib-only module to avoid the transitive pydantic/genai import chain.
---
The dev box runs Celery with `--concurrency=8` on 2 cores. Under a live scrape
(load avg ~47), importing ANY module that reaches `app.config` or `browser_pool`
triggers pydantic BaseSettings compilation, which takes 10–75s and hangs pytest
collection.

**Pattern: pure-module extraction.**
When a function only uses the stdlib (`re`, `os`, etc.) and needs to be testable
under any load:
1. Define it in a dedicated module with NO app imports (e.g. `challenge_shell.py`).
2. The "owner" module (`per_course_browser.py`) imports + re-exports it so callers
   that already import from there need no change.
3. Tests import from the pure module directly — collection completes in ~0.1s.
4. Register the validation command using the isolated test file so CI doesn't depend
   on scrape-free load conditions.

**Why:** pydantic BaseSettings compilation takes ~1s at idle but 10–75s at load 47.
`import google.genai` never completes within a 120s timeout at load 47. The only
reliable way to run tests under an active scrape is to import nothing from the
app chain.

**How to apply:** whenever you add a stateless regex extractor, text classifier,
or utility function to a module that has heavy transitive imports, check whether
it can be factored out into a no-app-deps module. Use `python -m py_compile` as
a quick check first (fast under any load); then test in isolation with
`PYTHONPATH=. python -m pytest <file> -c /dev/null -p no:cacheprovider`.
