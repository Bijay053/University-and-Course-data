# Portal assistant capability audit

## Failure demonstrated in the supplied conversation

The assistant loaded twenty pending rows but read only three course pages. It
did not locate an official English-requirements page when one was not configured.
This is a tool-orchestration limitation, not proof that OpenAI needs retraining.

The research path now includes up to forty sampled course pages for IELTS
requests, configured general/undergraduate/postgraduate English sources, and
bounded follow-up of official English links. Its deterministic coverage ledger
separates successful reads, unavailable pages and unexamined records. Page reads
are never described as proof of verified IELTS or a successful database correction.

The investigation remains time-bounded. Larger reviews and inaccessible pages
are reported as incomplete, not silently treated as checked.

## Comparison with a development agent

| Capability | Portal assistant | Remaining work |
|---|---|---|
| Understand review context | Current review, scoped conversation, recorded operations | Broader persistent task state and richer name resolution |
| Research official sources | Bounded verified requests and immutable excerpts | Multi-step retrieval, broader document support and complete large-cohort pagination |
| Execute repairs | Existing authorized pending-only operations run automatically | Broader field coverage and reliable multi-step continuation |
| Verify results | Operation outcomes and targeted read-back checks | Systematic per-field before/after evidence, bounded retries and coverage acceptance gates |
| Edit application code | Not available | Isolated checked-out workspace, scoped file tools and patch review |
| Run tests and debug software | Not available inside the portal chat | Controlled test runner, resource limits and machine-readable results |
| Roll back code changes | Not available inside the portal chat | Versioned patches/checkpoints with an audited recovery procedure |
| Release software | Not automatic | Release identity, regression gates and explicit production approval |

Replit's documented capabilities include code editing, dependencies, app testing
and checkpoints. Giving a model a similar prompt does not supply those tools.
Source: https://docs.replit.com/features/agent/overview

## Execution boundary for a future coding agent

Do not add unrestricted shell execution to the production API or scrape worker.
A code-changing agent needs a separate disposable execution environment with
no production credentials, a narrow repository scope, resource/network limits,
audited patches and test evidence. Production changes remain separately authorized.

## Verification scenarios

- Twenty-row English request is not truncated to three source pages.
- General and level-specific English pages remain distinct sources.
- Official English links are discovered without accepting external/private URLs.
- Failed and unexamined pages remain visible in coverage counts.
- Empty and oversized cohorts do not produce false completeness claims.
- Existing regression tests cover topic switches, permissions, stale actions,
  automatic dispatch, immutable evidence and pending-only mutations.

This audit is not a claim that the portal replaces Replit or that all scraper
errors have been resolved.
