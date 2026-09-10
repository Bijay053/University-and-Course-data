---
name: Pre-claim failure ownership fence
description: Ownership and connection rules for recording worker failures before a runtime job claim completes.
---

A worker failure whose claim did not complete may transition a runtime job only from `queued` to `failed`. The emergency write must use a fresh database connection outside the shared async SQLAlchemy pool.

**Why:** A claim failure can mean the normal pool is unusable, and the commit outcome can be ambiguous. Another worker may have successfully claimed the same row while the failing worker starts recovery. An unconditional failure update would overwrite that healthy owner.

**How to apply:** Treat claim execute or commit errors as a distinct pre-claim failure. Record them with one compare-and-set update fenced by queued status. Keep ordinary post-claim failures on the normal running-to-failed path.