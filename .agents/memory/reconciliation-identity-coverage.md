---
name: Reconciliation identity coverage
description: A reviewed evidence-component map can still conflict with the published offering identity.
---

A campus-course reconciliation needs two independent partitions: evidence
components (which prove historical ID relationships) and the application’s
published offering identity (which determines whether two proposed canonical
courses may coexist). Validate both before treating a reviewed map as
applicable.

**Why:** An exact, fully covered historical ID map passed the evidence preview
but failed the live read-only apply check because many separate reviewed groups
had the same official award, source route, degree and study variant. Their
published identities collided despite no conflicting fee or study-mode values.

**How to apply:** Inventory collisions under the exact application identity
function before asking for approval or making any write. If collapsing groups
would change the reviewed membership, obtain a revised exact approval; never
weaken the unique identity check or hide old IDs to force the apply through.

Also inventory singleton legacy published records against the incoming staged
review, not only duplicate groups.

**Why:** A successful duplicate reconciliation still left new approval blocked
by previously excluded singletons lacking a published offering identity. Those
records need a separately reviewed identity adoption, not a duplicate merge or
a bypass of the legacy-collision guard.

**How to apply:** Before promising that reconciliation unblocks the whole scrape,
run approval preflight for every incoming identity, including standalone awards.
Count public identities rather than physical campus evidence rows, and do not
force low-confidence records through merely to reach the displayed group count.

Singleton adoption must prove campus-to-fee correspondence independently of
matching scalar values and scope metadata, including each historical witness.

**Why:** A scope labelled London can still carry self-consistent Birmingham fee
options; metadata validation alone does not prove the fee belongs to that campus.
Also, row locks plus SERIALIZABLE do not universally block new competing rows
from ordinary application writers during an exhaustive inventory migration.

**How to apply:** Use the vetted campus fee planner or exact campus-label proof.
For one-off exhaustive migrations, acquire bounded table writer barriers before
the first snapshot-bearing query, then the normal university lock. Exercise
concurrent inserts in the isolated PostgreSQL canary; keep dry-runs truly read-only.