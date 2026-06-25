---
name: CPU-starvation genai/pydantic import stall in dev
description: Why cold google.genai/pydantic imports and pytest collection hang in the dev box, and how to run tests anyway.
---
On this 2-core dev box the running workflows (Celery `--concurrency=8` + uvicorn
`--reload` + 2 Vite dev servers + Redis) can drive load average to ~36. Under
that load:
- `import pydantic` alone takes ~10s (normally <1s); `import google.genai`
  (heavy pydantic-model compilation) does NOT finish within 75s.
- `pytest` collection via `tests/conftest.py` (`from app.main import app`) pulls
  the whole app incl. google.genai and blows past the 120s tool budget.

**This is CPU starvation, not a deadlock or a code bug** — the live workers
already have everything imported and serve fine. It clears when a big scrape
finishes (observed load fall 36 -> 4 within minutes).

**How to run tests under load without waiting for it to clear:**
- `google.genai` is imported *lazily* inside `gemini_client` functions, and unit
  tests mock `_client`, so the heavy SDK is never needed at import time. Only
  `tests/conftest.py` forces it.
- Run isolated copies from a dir with no conftest:
  `cp tests/<files> /tmp/x && PYTHONPATH=. python -m pytest /tmp/x -c /dev/null
  -p no:cacheprovider -q` (skips conftest + pyproject).
- For tests that call `gemini_client.generate()` (which does
  `from google.genai import types`), stub it BEFORE importing gemini_client, but
  only when `"google.genai" not in sys.modules`: register fake `google.genai` +
  `google.genai.types` modules with a no-op `GenerateContentConfig`. In the full
  suite the real SDK is already loaded, so the stub self-skips and real types
  are used. Never stub the `google` namespace package itself.

**Why:** repeated 120s tool-timeouts on cold imports waste turns; the fix is to
bypass conftest and stub the lazy SDK, or just wait for scrape load to drop.
