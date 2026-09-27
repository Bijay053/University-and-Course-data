---
name: Approval guidance lifetime
description: Why failure advice must be invalidated permanently after evidence changes
---

Last-attempt advice belongs to an uninterrupted row/evidence state, not merely matching current values.

**Why:** Hiding a failure by comparing fingerprints alone lets that old advice reappear if an edit, recovery, or backup later restores the original values. A prior failed attempt is not evidence about the restored state, and never authorizes approval.

**How to apply:** Clear guidance durably when evidence changes, including bulk/background writes. Retain exact row/job identity and stale-write fencing for delayed failure reports. Keep unrelated rows' advice intact; shared database sessions are not shared evidence identity.