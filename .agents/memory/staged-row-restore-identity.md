---
name: Staged-row restore identity
description: Identity rules for restoring exact staged-row backups versus legacy extraction snapshots.
---

Exact staged-row backups must use canonical course URL identity when deciding whether a review row already exists. A same-name row at another URL must not suppress restoration. Keep name-based fallback only for legacy extraction snapshots, whose fetched URL may differ from the final canonical course URL.

**Why:** Course titles are not unique, and interrupted runs can leave unrelated same-name rows behind. Exact backups already contain the final persisted review URL, while legacy snapshots may only know a transport or fetched URL.

**How to apply:** When changing review restoration or deduplication, align exact backups with the database's canonical-URL identity constraint. Use broader name matching only where snapshot fidelity cannot provide reliable URL identity.