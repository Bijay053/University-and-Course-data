# One-click AI repair

The existing AI repair action now starts a server-managed workflow:

1. Inspect a bounded sample of live official pages.
2. Propose supported configuration repairs.
3. Test against saved evidence and freshly fetched course pages without changing course rows.
4. Save only accepted changes, protected against concurrent configuration edits.
5. Run one isolated verification scrape and compare its measured quality with the source run.

## Limits and scope

- At most five AI proposals, with 45 seconds and 2,048 output tokens per call.
- At most 12 live page requests within a 180-second probe window, with a 20-second request limit.
- One verification job, capped at 50 courses and 600 seconds.
- Verification stops on the existing observed course-extraction Gemini cost monitor with a $2 ceiling. This is **not** a total spending cap: discovery, repair AI, other providers, and already-in-flight requests are not all covered by that monitor.
- Lost deliveries can be requeued twice, always using the same durable session or child job identity.

The interface reports **Bounded sample verified**, **Needs review**, or **Blocked**.
Saving a configuration does not prove that a repair worked. A capped, unmeasured,
degraded, or incomplete run is not marked verified, and even a clean sample does
not certify that the full university catalogue was found.

## Data protection

Verification does not reuse old checkpoints, inherit approved course values,
replace another run's pending review rows, or publish courses. It creates fresh,
job-scoped rows that remain pending for human review. The original review set and
published courses remain intact.

Repair ownership, attempts, live evidence, verification identity and comparisons
are retained in the durable audit. Status polling and an independent periodic
reconciler recover queued deliveries; reopening the page does not launch another
scrape. A stalled claimed worker is not blindly rerun.

## Supported repairs and honest stopping conditions

Automatic application currently covers evidence-backed URL filtering changes
and supported extraction selectors that pass preservation and authority checks.
It is not arbitrary AI-generated scraper code.

Audience-scoped repairs may also use a typed international intake recipe and a
linked official English-requirements source. These rules are derived from live
DOM evidence rather than trusted from the AI proposal: the international and
domestic options must be balanced, their values must belong to the same
selector panel, and the linked source must stay on an approved official host.
The same typed rule is replayed from saved evidence and used during isolated
verification. It cannot create universal English-score or intake-date defaults.

Ambiguous audience labels, image-only controls, conflicting institutional
requirements, unbalanced samples, cross-audience writes, or a rule that neither
fills nor safely corrects a value result in **Needs review**. Applied recipes,
their live evidence, linked-source provenance and rollback configuration are
retained in the durable audit. Course publication remains manual.

Catalogue-provider, sitemap, browser strategy and crawl-budget changes still
need a safe provider-replay validator before automatic application. Missing
snapshot baselines, unsupported page templates, unverified redirects/hosts,
blocked sources, ambiguous international tuition or English requirements, and
exhausted budgets stop automatic application rather than fabricate data.

Offline regression tests cover live-gate failures, stale cache state, concurrent
ownership, duplicate deliveries, budgets, isolated staging, and UI outcomes.
A fresh paid, end-to-end university scrape is a separate operational validation;
the implementation tests do not assert that every live university site can be
repaired automatically.