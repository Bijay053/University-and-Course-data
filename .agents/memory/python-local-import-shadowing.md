---
name: Python local-import shadowing
description: Prevent misleading lock-contention failures caused by function-local SQL helper imports.
---

Do not reuse a module-level helper name in an import or assignment later inside a long function. Python marks that name local for the entire function, so earlier references raise `UnboundLocalError`.

**Why:** A completed scrape left a valid Redis university lock during final cleanup. Its recovery run should have recognized the holder as terminal, but a late local SQL alias broke the earlier status lookup. The broad exception handler then made the visible event look like a genuine duplicate scrape.

**How to apply:** Give late imports unique aliases or keep shared helpers at module scope. For lock checks that catch lookup errors, inspect the persisted job error as well as the user-facing contention event before concluding that the lock owner was active.