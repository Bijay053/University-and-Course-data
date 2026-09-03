---
name: Live catalogue removal safety
description: Safety rule for reconciling published courses against a newly approved scrape.
---

Never automatically deactivate or delete a live course solely because it is
absent from one completed or approved scrape. Treat removal as an explicit,
permission-gated, audited review action, and prefer reversible deactivation over
deletion. Compare distinct linked course IDs and surface the exact additions and
removals for an operator to confirm.

**Why:** A full approval can legitimately contain fewer rows than the live
catalogue, but absence is ambiguous: it may mean retirement, an intentional
eligibility rejection, temporary under-discovery, or a failed promotion.
Automatic reconciliation can therefore hide valid courses even when a simple
count threshold passes.

**How to apply:** When approved and live counts diverge, identify the exact
records and source-backed reason first. Any future bulk reconciliation flow must
require authorization, all-row linkage checks, an explicit removal confirmation,
an audit record, and transaction locking shared with course promotion.