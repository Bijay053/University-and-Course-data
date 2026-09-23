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

A dated URL is not enough evidence to merge a course into a current sibling or strip its archive identity.

**Why:** Winchester's year-suffixed MA route serves a current title, while its explicit year-directory research route remains an archive. Treating both as identical title-cleaning cases can create indistinguishable duplicates.

**How to apply:** Follow course-owned official titles, preserve explicit archive years, and require review of dated routes rather than automatically publishing or removing possible duplicates.

Counterpart evidence must certify the exact source URL, not just a stable database row identity.

**Why:** A reviewer can edit the course URL while an official-page fetch is pending or after evidence was saved. Row/job/university checks alone then allow evidence from the old page to certify the new page.

**How to apply:** Revalidate source identity and review revision under a fresh database lock before persisting evidence or decisions. Invalidate active decisions on URL edits while retaining audit history; test edits during fetching in separate sessions.