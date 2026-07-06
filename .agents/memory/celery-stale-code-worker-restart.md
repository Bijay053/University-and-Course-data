---
name: Celery worker stale-code trap
description: A scraper/orchestrator bug fix landed on disk but production behavior is unchanged — check whether the long-running worker process actually reloaded the code before re-debugging the fix.
---

Celery workers (and any other long-running Python worker process — FastAPI/uvicorn without `--reload`, background daemons) load application code once at process start and do NOT pick up file changes on disk. Committing a fix to git or having it present in the working tree is not sufficient for it to take effect — the process must be restarted.

**Why:** After landing a discovery-logic fix for a scraper bug, a fresh scrape job kicked off ~15 minutes after the commit still reproduced the *exact* pre-fix symptom (same low per-page candidate counts). Directly invoking the fixed function in an isolated script reproduced the *correct* post-fix behavior immediately using the identical config and URL — proving the code itself was correct. Comparing `ps -eo pid,lstart,cmd` (worker process start time) against `git log` (fix commit time) showed the worker process had started ~30 minutes *before* the fix commit. The long-running worker was still executing the stale in-memory module.

**How to apply:** Whenever a fix to code that runs inside a long-running worker (Celery, background workflows, non-reloading servers) doesn't show up in production behavior despite being verifiably correct in isolation, check the worker process start time vs. the fix commit time before spending more effort re-debugging the fix itself. If the process predates the fix, restart the workflow/worker and re-test — do not assume the fix is wrong. This applies generally any time "my fix isn't taking effect" — verify the runtime actually loaded the new code before doubting the fix.
