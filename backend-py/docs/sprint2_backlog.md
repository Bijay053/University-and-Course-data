# Sprint 2 Backlog (post Week 5 planning checkpoint)

Realistic Sprint 2 capacity for one engineer over 3-4 weeks: **2-3
items** from the table below.  Pick after reviewing Week 4-5 outcomes.

| Item | Status | Effort | Impact | Sprint 2 candidate? |
|------|--------|--------|--------|---------------------|
| Sibling-cache replacement | Deferred — current is now traceable but architectural concerns remain | M (2 wk) | High (data integrity) | **Yes if** ≥3 unis show systematic wrong-bucket sibling-cache rows |
| Cross-scrape caching | Deferred — sibling cache covers within-scrape | M (2 wk) | Cost reduction | **Yes if** Week 4-5 cost projection shows >$X/month at 80-uni scale |
| Embedding-based fuzzy matching | Deferred — CRICOS works for AU | L (3-4 wk) | Required for international expansion | **No** — block on NZQA + after Sprint 2 |
| NZQA matching for NZ unis | Deferred — not needed for AU completion | M (2 wk) | Required for NZ expansion | **No** — only when AU is at >60 unis |
| Dashboard UI | Deferred — SQL views suffice | L (3-4 wk) | High for ongoing ops | **Yes if** spot-check workflow took >50% of Week 4-5 op time |
| UTAS bug fixes (locations, over-blocking, online_only) | In progress | S (3-5 d) | Required to approve UTAS | **Yes** — small enough to fit alongside one larger item |
| Production data remediation (UTAS 173 rows + AU Nursing IELTS audit) | Deferred | M (1-2 wk) | High for trust | **Yes if** any other production-data wrongness surfaced in Week 4-5 |
| CRICOS coverage gap (CSU / UOW / VIT showing 0%) | Open — extractor wired but no matches on those sites | S (1 wk) | Medium | **Probably yes** — affects CRICOS reporting accuracy |
| Sibling-cache cascade failure root-cause (Week 5 fix was a band-aid) | Surfaced in Week 5 | S (1 wk) | Medium | Optional — current fix is sufficient if no recurrence |

## Decision framework (Step 3 from spec)

- **Must do**: anything blocking >50% of remaining 50 unis from being
  onboarded.
- **Should do**: anything cutting operational cost (engineer time or
  Gemini cost) by >25%.
- **Could do**: new feature work that opens new uni segments
  (NZQA for NZ, embedding for international).

## Process changes for Sprint 2 (retrospective)

What worked in Sprint 1:
- Per-uni YAML pattern + Pydantic schema validation.
- Verification gates pre-merge (preflight scripts).
- Patterns doc capturing reusable platforms.

What didn't work in Sprint 1:
- "Code is done" claims without verification SQL output (Week 1
  Prompt 1 zero-rows problem).
- Silent skipping of regression-sweep gate on shared-code changes
  (six occurrences across Sprint 1).

Process mandates for Sprint 2:
1. **No "done" without verification.**  Every PR description must
   include the output of the verification SQL or test, pasted inline.
2. **Sweep before promote on shared code.**  Any change touching
   `defaults.yaml`, `extractors/`, or `pipelines/` must include the
   regression-sweep diff (paste + zero-regressions confirmation).
3. **Architecture doc is mandatory output.**  Every shipped feature
   updates `docs/architecture.md` in the same PR.

## Pick-list template

After Week 5 closeout, fill in:

```markdown
## Sprint 2 picks (decided YYYY-MM-DD)

1. <item> — chosen because <rationale tying to Week 4-5 evidence>
2. <item> — chosen because <rationale>
3. <item> — chosen because <rationale>

## Explicitly NOT picked, and why

- <item> — <reason: lower priority, higher cost, etc.>
```
