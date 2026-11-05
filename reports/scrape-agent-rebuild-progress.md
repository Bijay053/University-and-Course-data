## Latest workspace checkpoint — 2026-11-05

- User approved USD100/day. Dollar reservations are serialized across workers; unknown usage retains reservations; cost-bound overruns freeze subsequent calls. Stops now produce a chat response without automatic retry. Reset is midnight UTC.
- Owner allowlist is implemented, but neither global activation nor production-user activation has been performed.
- Shared provider/job row counts are integrated. Last read-only Excelsia inventory was 48 pending rows across four jobs (23 + 3 + 21 + 1); a single-review display is not the provider total.
- Staff-location undo restores prior values/provenance, retains audit history, and refuses later-edited rows.
- An official CRICOS registry read tool now exists. Missing codes, mismatched identities and expired results remain unverified; registry prices never become current tuition.
- Existing 52 research candidates were exported as an unapproved team-review spreadsheet. This is not the final partner-based, fully verified golden set.

### Checks and limitations

- Broader regression: 329 passed before the latest isolated additions.
- Latest source-batch/CRICOS/loop/location checks: 22 passed.
- Latest dollar-budget/access/concurrency/stop checks: 8 passed.
- Frontend typecheck passed; 27 assistant component tests passed.
- Real-model count-tool and budget smoke checks passed. These are not full acceptance tests.
- Real-model six-row Excelsia rehearsal FAILED acceptance. A token-budget failure and integer-array validation defect were identified; after fixing the argument validation and tool guidance, all six source checks ran, but no verified intake evidence was produced and no intake was changed.
- The rehearsal used disposable PostgreSQL and live official pages, not production writes. See reports/excelsia-real-agent-rehearsal.json for exact results.
- Full authenticated browser acceptance is not yet verified.
- Global policies/undo/application, full schema integration, broader published-field confirmation, self-repair rule persistence, complete behavioural coverage for all 85 scenarios and full change monitoring remain incomplete.
- Real-model CRICOS integration passed against the live government register, matching institution, award and registration code without changing data or inferring current admission availability. See reports/cricos-real-model-rehearsal.json.
- No AWS release or production flag enablement was performed. No Sydney changes or six-row intake changes were applied to production.

# Scrape agent rebuild — implementation checkpoint

This is **not a completed rebuild or a release approval**.

## Governing inputs

- `attached_assets/Scrape_Agent_Rebuild_Brief_1793843491960.pdf`
- `attached_assets/University_Scraping_Scenarios_1793843496913.pdf`

Follow the six stages in brief section 8. Every scenario row needs a behavioral test;
passing existing regression tests is not equivalent to passing that specification.

## Live baseline observed

Read-only AWS inspection, before any new-agent deployment:

- Checkout: `3a8734b4e93a0e398fa517bd58edda1565d6c507`.
- Master of Counselling: one pending row, 56075, intakes February/July, audit 4.
- Row 56064: rejected with the explicit official domestic-only reason.
- 27 pending Excelsia rows.
- Empty intakes: 56052, 56026, 56053, 56063, 56073, 56042.
- Excelsia `assistant_rules` empty.

These observations are not live end-to-end chat acceptance results.

## Workspace implementation

New `scrape_agent_v2` package, disabled by default through `scrape_agent_v2_enabled`:

- OpenAI Responses tool-call loop, tool result receipts, token/round ceilings.
- Scoped database reads, audit reads and existing-rule reads.
- Literal intake edits without fetching; source-backed field-edit primitives.
- Atomic small batches, approval proposals, conservative duplicate exclusion and undo.
- Schema contracts for evidence, fees, intake metadata, duration ranges and English variants.
- Web/PDF reading, browser transport interception, robots checking, request throttling,
  and conversation page caching including unavailable sources.
- Database-only status questions reject web tool calls.
- Typed provider-rule save/remove, per-course batch checkpoints and bounded queued continuations.
- Engine identity pinned per turn; interrupted work does not switch to the legacy engine.
- Cumulative ten-row write cap prevents splitting a large operation into small calls.
- Legacy pipeline remains the default.

## Important incomplete work

Do not turn the feature flag on for production yet.

- Full schema/storage/UI integration for the new domain contracts.
- Complete field/row scope ambiguity, evidence-conflict and mutation regression matrix.
- Confirmation execution for all large batches and published-field edits.
- Global rules, policy undo and fully verified immediate batch rule application.
- End-to-end worker-loss/continuation testing beyond the focused queue/ownership tests.
- Explicit cache refresh requests and full site-search/provider profile behavior.
- Self-fixing extraction rules and integration into normal future scrapes.
- Official eligibility-register tools.
- Every companion scenario mapped to a concrete automated behavioral test.
- Independently verified 50-course golden set, including AU/UK/US/Canada,
  with actual per-field benchmark results.
- Scheduled change monitoring, durable daily spend accounting and price-based limits.
- Full browser, real-model tool integration and production acceptance tests.
- AWS release and old-pipeline retirement.

The token limit is not a dollar-denominated daily spend limit.
Queued continuations and persisted transcripts have focused tests, not a complete live recovery acceptance result.
The intake domain contract is not yet a migration of all legacy records.
No current code was pushed or deployed as part of this rebuild checkpoint.

## Checks

The current combined run passed **85 targeted/regression tests** in 109.57 seconds,
with one existing Pydantic deprecation warning. A first run hit the shell time limit;
the completed run above is the reported result. A real model/Responses tool-call
round trip also passed. These are not all PDF scenarios.

The companion PDF inventory contains **76 scenario rows and 9 country rows**.
Research collected **52 course candidates**, not an approved 50-course golden set.
Coverage and source applicability gaps remain; no per-field agent accuracy is
claimed before actual benchmark observations exist.

The research excerpt checker confirms 179/179 claimed snippets exist in saved
official sources after correcting non-contiguous quotations. This does **not**
verify all field interpretations or make the candidates an approved golden set.

FastAPI and Celery restarted successfully. The public page renders; signed-in
assistant UI and full live chat acceptance were not browser-verified.
The standalone readiness checker returns `ready: false`; it is not yet wired
into every AWS release path.
