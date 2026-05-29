# Architecture — Sprint 1 (Weeks 1-5)

This document captures what was built in Sprint 1 and which invariants
must be preserved going forward.  Update when shipping anything that
changes one of the invariant categories below.

For the operator-facing field reference and cookbook see
`docs/YAML_OPERATIONS_GUIDE.md`.  For known failing tests see
`KNOWN_ISSUES.md`.

---

## What we built in Sprint 1

### Data correctness layer (Week 1-2)
- Sibling-cache provenance tracking (`scraped_field_evidence.snippet`).
- AI-fallback override block — AI cannot replace regex/css/gemini_primary values.
- Vision OCR path block-list + page-text validation (false-positive control).
- Sibling cache source-type gating + ≥2 source consensus.
- Skip central-English propagation (Week 2 Prompt 5).
- Lower sanity floors for pathway programs (Week 2 Prompt 6).
- Don't revert vision values found verbatim in page text (Week 2 Prompt 7).

### Observability layer (Week 2-3)
- `scrape_run_metrics` table (per-uni / field / method, with `fill_rate`).
- `university_field_baselines` table + nightly refresh (02:00 UTC).
- Alert evaluator with rule_id-keyed `scrape_run_alerts` rows.
- `gemini_call_log` (per-call: model, tokens, cost, duration, success).
- Reporting views: `v_gemini_cost_by_university`,
  `v_gemini_cost_by_call_type`, `v_gemini_top_spenders_30d`,
  `v_gemini_skip_efficiency`.

### Cost optimisation (Week 3 Track A)
- Skip-Gemini gate (`gemini_gate.py`) — downgrade to cheap classification
  when other extractors already covered ≥90% of high-value fields.
- Quota tracker + circuit breaker (5 errors / 60 s → open 5 min).
- Per-job + per-uni cost ceilings (`cost_ceiling.py`).
- Confirmed `gemini-2.5-flash-lite` is cost-optimal (audit script in
  `scripts/check_gemini_model.py`).

### CRICOS matching (Week 3 Track B)
- `extractors/cricos_code.py` extractor.
- PDF-pipeline integration with `cricos_match` provenance suffix.
- `pipelines/university_pdfs.py` writes `extraction_method` =
  `uni_pdf:cricos_match:fees` / `uni_pdf:cricos_match:requirements` at
  authority tier 2.5.
- Reporting: `v_cricos_coverage_au` (migration 018).

### Per-uni YAML config system (Week 1 onwards)
- `scraper_config/defaults.yaml` — global defaults; protected by
  regression-sweep policy.
- `scraper_config/unis/<slug>.yaml` — per-uni overrides.
- `app/services/scraper/config/` package: schema (Pydantic `UniConfig`),
  loader (deep-merge: defaults → DB `scrape_config` → per-uni YAML),
  context (`ContextVar` scoped to each scrape job).
- Tier-3 playback uses `for_tier3_replay()` to strip `extraction:` so
  per-uni filter assumptions cannot contaminate unknown-uni scrapes.
- **Discovery fields shipped this Sprint** (all per-uni opt-in via YAML):
  `must_contain`, `allow_url_patterns`, `block_url_patterns`,
  `fallback_subdomains`, `bfs_page_budget`, `max_candidates`,
  `always_sitemap_supplement`, `always_browser_discover`,
  `use_stealth_browser`, `use_wayback`, `extra_course_urls`.
- **Extraction fields shipped this Sprint** (all per-uni opt-in):
  `fees.force_central_fee_stage`, `fees.fees_pdf_url`,
  `fees.prefer_annual_over_total`, `fees.prefer_year_one_over_total`,
  `fees.pdf_overrides_page_regex`, `fees.pdf_parser`,
  `fees.pdf_row_pattern`, `fees.pdf_fee_term`, `fees.course_pdf_aliases`,
  `english.trust_tier1_vision_ocr_english`, `english.test_blocklist`,
  `intake.rolling_enrollment_label`,
  `text_cleaning.duration.reject_sentence_patterns`,
  `text_cleaning.global_substring_blocklist`,
  `text_cleaning.field_overrides`,
  `course_name.strip_title_suffixes`,
  `url_rewrites`, `default_course_location`, `max_parallel_fetch`.

### Promotion path hardening (Week 5)
- `approve_scraped_course` raises a clear `ValueError` on empty
  `course_name` instead of crashing on `None.lower()`.
- `bulk_approve.py` calls `db.rollback()` in its per-row exception
  handler — fixes the cascade-failure bug where one bad row poisoned
  the session and made every subsequent row fail.

### Production scale (Week 4-5)
- Top 10 AU universities — prep pack shipped (Week 4).  UWA is the
  most fully-tuned example YAML in `scraper_config/unis/uwa.yaml`
  (not a stub — it carries force_central_fee_stage, prefer_year_one_over_total,
  must_contain, reject_sentence_patterns, and course_name overrides
  tuned across multiple live scrape iterations).
- Next 20 AU universities — stub YAMLs + ranked list (Week 5).

---

## Architecture invariants

These constraints must hold for any change to merge.

1. **Defaults policy.**  Any change to `scraper_config/defaults.yaml`
   requires a full regression sweep across all baselined unis.
2. **Gemini logging.**  Every Gemini API call must log to
   `gemini_call_log` (no silent calls).
3. **Provenance.**  Every sibling-cache write must include the source
   snippet in `scraped_field_evidence.snippet`.
4. **Per-uni quirks belong in YAML.**  Per-uni hostname-based if-blocks
   in shared code are deprecated; new behaviours go in
   `scraper_config/unis/<slug>.yaml`.
5. **AI-fallback authority.**  `ai_fallback` extraction method may not
   override values produced by `regex`, `css`, or `gemini_primary`.
6. **Vision-OCR validation.**  Vision OCR values require keyword + URL
   path + page-text validation before being staged.
7. **Promotion safety.**  `approve_scraped_course` must validate input
   before opening the transaction; `bulk_approve.py` must rollback on
   per-row exception.

---

## Production deployment — permanent trip wires

These are not fragilities to clean up — they are deliberate workarounds for
fixed conditions of this server.  Changing them will break production.

### DB URL must use `127.0.0.1`, NOT `localhost`
**Why:** This DigitalOcean droplet's internal DNS (`getaddrinfo("localhost")`)
fails with `Errno -3 Temporary failure in name resolution` when asyncpg
attempts SSL hostname verification.  Using the IP literal `127.0.0.1` bypasses
the DNS lookup entirely.  The hardcoded default in `app/config.py` is already
set correctly.  Do NOT "clean up" this to `localhost` — it will break
every async DB connection in production.

### Do NOT run `alembic upgrade head` on production
**Why:** Same DNS issue — alembic's connection string goes through TCP to
`localhost`, which triggers the same `Errno -3` failure.  Apply all schema
changes via direct `psql` instead:
```bash
sudo -u postgres psql -d university_portal -c "ALTER TABLE ..."
```
The `alembic_version` table on production contains manually-inserted fake
version IDs that do not match actual migration filenames.  Ignore alembic
version tracking on production entirely.

---

## Known limitations (Sprint 2 candidates)

See `backend-py/docs/sprint2_backlog.md` for the prioritised backlog.
See `KNOWN_ISSUES.md` for currently failing tests.

---

## Reference docs

- `docs/YAML_OPERATIONS_GUIDE.md` — **operator field reference and cookbook** (start here).
- `docs/uni_onboarding_patterns.md` — site-platform patterns and per-uni status table.
- `docs/week4_scale_up_log.md` — top-10 status + per-uni run log.
- `docs/sprint2_backlog.md` — Sprint 2 candidate items with effort / impact estimates.
- `scraper_config/unis/` — per-uni YAML library.  `uwa.yaml` is the most complete example.
- `scripts/week4_*` and `scripts/week5_*` — operational scripts.
- `KNOWN_ISSUES.md` — pre-existing test failures and their root causes.
