---
name: Agent dollar budget
description: The approved cap and fail-closed accounting rules for the rebuilt scrape agent.
---
The user approved a US$100 daily agent cap before production enablement. Account across users/workers, with an explicitly disclosed UTC-day reset.

**Why:** Concurrent workers and uncertain API failures must not bypass the monetary limit. A failed request can still be billable.

**How to apply:** Reserve conservative cost before each model request, retain reservations when usage is unknown, and freeze further calls if usage exceeds its bound. A new model needs verified pricing; do not silently reuse another model's rates. Passing mocked budget tests does not establish live intake-repair acceptance.
