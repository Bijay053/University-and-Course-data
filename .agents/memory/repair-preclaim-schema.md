---
name: Repair pre-claim schema readiness
description: A repair stuck at attempt zero can be a delivered worker crashing before its durable claim.
---

Do not diagnose absent durable claims as broker delivery failures without checking worker receipt and initialization logs.

**Why:** Production received the initial repair and both redispatches, but all crashed on missing worker-fencing schema before claiming. API health and release identity checks still passed.

**How to apply:** Check actual schema prerequisites before admitting repairs and before deploying fencing-dependent code. A lagging migration marker requires inventory and a scoped reviewed migration, not blindly upgrading through unrelated migrations or stamping history.