# Autonomous Scraping Pipeline — Architecture

> **Goal**: Any client enters a university website URL. The system probes it,
> selects the right strategy and library stack, generates config, runs the
> scrape, validates quality, and self-heals — with zero YAML or manual
> configuration required.

---

## Pipeline Stages

```
URL entered  (UI "Add by URL" → POST /api/universities/add-by-url)
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Stage 1 — Site Probe                           │
│  site_probe.probe_site()                        │
│                                                 │
│  Detects:                                       │
│  • Cloudflare / WAF block                       │
│  • JS SPA (React / Vue / Angular)               │
│  • Hidden search APIs (SearchStax, Algolia …)   │
│  • CMS platform (Phase 4A)                      │
│    wordpress / wordpress:elementor / drupal /   │
│    terminalfour / moderncampus / courseleaf /   │
│    sitecore / sharepoint / joomla / silverstripe│
│  • Sitemap + course URL count                   │
│  • Wayback archive availability                 │
│                                                 │
│  Outputs: SiteProfile (incl. cms_platform)      │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 2 — Strategy + Library Selection         │
│  library_strategy.recommend_library_stack()     │
│                                                 │
│  Strategies:                                    │
│    static_html  → requests + parsel             │
│    browser      → Playwright                    │
│    wayback      → httpx + Wayback CDX           │
│    search_api   → generic_search_api provider   │
│    proxy        → curl_cffi (TLS fingerprint)   │
│    blocked      → Wayback fallback              │
│                                                 │
│  Outputs: LibraryStack (situation key)          │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 3 — Platform Fingerprinting (Phase 4A)   │
│  auto_config_generator._derive_platform_type()  │
│                                                 │
│  Priority:                                      │
│    1. API provider  (searchstax, algolia …)     │
│    2. CMS fingerprint (wordpress:elementor …)   │
│    3. library_stack.situation                   │
│    4. recommended_strategy fallback             │
│                                                 │
│  Outputs: platform_type key                     │
│  Used as pattern_store lookup key (Phase 3)     │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 4 — Pattern Lookup (Phase 3)             │
│  pattern_store.get_patterns_for_platform()      │
│                                                 │
│  Checks scraper_patterns table for proven       │
│  CSS/XPath/regex rules on this CMS.             │
│  If found, seeds Gemini prompt with them.       │
│  → Fewer Gemini tokens, faster, more accurate   │
│                                                 │
│  Outputs: seeded extraction rules               │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 5 — Auto-Config + AI Rule Generation     │
│  auto_config_generator.generate_config()        │
│  ai_extractor_gen.generate_extraction_rules()   │
│                                                 │
│  • Heuristic rules fill easy cases              │
│  • Gemini analyses probe + sample HTML          │
│  • Stored in universities.scrape_config         │
│    ["auto_config"] (JSONB)                      │
│  • Stored keys:                                 │
│    _platform_type, _library_situation,          │
│    _strategy, _probe_summary                    │
│                                                 │
│  Outputs: UniConfig (merged with per-uni YAML)  │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 6 — Scrape + Extract                     │
│  orchestrator.run_scrape()                      │
│                                                 │
│  Discovery:                                     │
│    BFS → sitemap → alt-paths → subdomain        │
│    probes → browser → Wayback                   │
│  API short-circuit if _api_provider set:        │
│    generic_search_api.fetch_generic_api_links() │
│  Extraction: per-course CSS/XPath/regex/AI      │
│  Staging: scraped_courses + field evidence      │
│                                                 │
│  Outputs: staged courses in scraped_courses     │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 6b — PDF Intelligence (Phase 6)          │
│  pdf_link_discoverer + pdf_classifier +         │
│  entry_req_extractor + university_pdfs.py       │
│                                                 │
│  Auto-discovery (no YAML required):             │
│    • Probes /fees /admissions /entry-req paths  │
│    • Scores PDF links by URL+anchor+context     │
│    • Suppresses low-value PDFs (privacy policy, │
│      annual reports, forms, complaints, etc.)   │
│                                                 │
│  Classification (8 categories):                 │
│    fee_schedule · entry_requirements · handbook │
│    prospectus · course_catalogue · intake_cal   │
│    scholarship · other                          │
│    Gemini fallback only when confidence < 0.50  │
│                                                 │
│  Extraction:                                    │
│    Fees → international_fee                     │
│    English → english_test / ielts_overall       │
│    Entry requirements → ATAR, GPA, prior degree │
│    work_exp, portfolio/interview, prerequisites │
│    → merged into other_requirement              │
│                                                 │
│  Caching:                                       │
│    Discovered PDFs stored in                    │
│    auto_config["_discovered_pdfs"] — reused on  │
│    subsequent scrapes without re-discovery      │
│                                                 │
│  Quality gate (post-loop):                      │
│    After main extraction loop, checks fill      │
│    rates.  If other_requirement < 30% OR        │
│    international_fee < 50%, re-discovers PDFs   │
│    and backfills affected staged courses.       │
│    Gemini Vision used only when text extraction │
│    fails (pdf_vision.py fallback).              │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 7 — Quality Analysis (Phase 5)           │
│  quality_intelligence.build_quality_report()    │
│                                                 │
│  Per-field fill rates + root-cause diagnosis:   │
│    other_requirement 0.18 → "Often in PDFs /   │
│      behind JS" → "Enable browser pass"         │
│    international_fee 0.95 → ✓ good              │
│                                                 │
│  API: GET /api/universities/{id}/quality-report │
│  Outputs: issues[], platform_hints[],           │
│           recommended_actions[]                 │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 7b — Quality Action Dispatcher (Phase 7) │
│  quality_action_dispatcher.dispatch_quality_    │
│  actions() + scrape_tasks.run_quality_actions   │
│                                                 │
│  Fires in the 70–84 % completeness gap —        │
│  above CASCADE's repair floor but below the     │
│  85 % auto-publish gate.                        │
│                                                 │
│  Actions (priority order):                      │
│  1. PDF extraction (inline) — backfills         │
│     international_fee, other_requirement,       │
│     english_test (ielts_overall), academic_score│
│  2. repair_extractor (Celery) — AI rule regen   │
│     for degree_level, study_mode, duration …    │
│  3. browser_retry (Celery) — Playwright re-run  │
│     for JS-SPA sites                            │
│                                                 │
│  Safety guarantees:                             │
│  • Never overwrites fill_rate ≥ 80 % fields     │
│  • Each ActionType dispatched once per run      │
│  • Max 2 Celery tasks per run                   │
│  • Skips repair_extractor if CASCADE fired it   │
│  • Entire block try/except — never kills job    │
│                                                 │
│  Action log persisted in universities           │
│    .scrape_config['_p7_last_run']               │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 8 — Self-Heal CASCADE                    │
│  orchestrator (end of run_scrape) +             │
│  scrape_tasks.probe_and_configure               │
│                                                 │
│  [discovery_failure]  staged < 5:               │
│    → probe_and_configure.delay(                 │
│        exclude_strategies=[current_strategy])   │
│                                                 │
│  [extraction_failure] avg < 70%:                │
│    → repair_extractor.delay(uni_id)             │
│                                                 │
│  No cascade if per-uni YAML exists (safety)     │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│  Stage 9 — Pattern Promotion (Phase 3)          │
│  pattern_store.promote_patterns()               │
│                                                 │
│  After repair: successful CSS selectors stored  │
│  in scraper_patterns keyed by platform_type.    │
│  Running average of confidence across all       │
│  universities on the same platform.             │
│                                                 │
│  THE FLYWHEEL:                                  │
│    WordPress Uni A → repair → promote           │
│    WordPress Uni B → starts with proven rules   │
│    → higher completeness → less Gemini          │
│    → fewer repairs                              │
└─────────────────────────────────────────────────┘
```

---

## Key Design Invariants

1. **Per-uni YAML always wins** — auto_config sits between global defaults and
   per-uni YAML in the merge order. A hand-tuned YAML file is never overwritten.

2. **CASCADE never overwrites existing YAML** — safety check at cascade entry.
   The 43+ hand-tuned files are untouched.

3. **Pattern promotion is additive** — running average; never deletes existing
   patterns. Confidence can only increase (with more evidence) or stay stable.

4. **Gemini is optional at every stage** — heuristic rules run first; Gemini
   is only called when heuristics can't fill the gap. The skip gate suppresses
   Gemini calls entirely when other extractors achieve ≥ 90% fill at ≥ 0.70
   confidence.

5. **Platform_type key is stable** — once derived for a university, changing
   the CMS/platform requires a re-probe, not a code change.

6. **Quality threshold hierarchy**:
   - CASCADE triggers at avg < 70%
   - Auto-publish gate at 85%
   - Quality report warns at < 80% per field, diagnoses at < 40%

7. **Duplicate detection** — `add-by-url` checks for existing universities by
   hostname match before creating a new record.

---

## Platform-Type Key Reference (Phase 4A)

| Key | Detection source | Notes |
|---|---|---|
| `searchstax` | API provider | HUD, SearchStax-hosted |
| `algolia` | API provider | Algolia search unis |
| `wordpress:elementor` | CMS fingerprint | Most common AU/UK WP build |
| `wordpress:acf` | CMS fingerprint | ACF-heavy WordPress |
| `wordpress:divi` | CMS fingerprint | Divi / Elegant Themes |
| `wordpress` | CMS fingerprint | Plain WordPress |
| `drupal` | CMS fingerprint | Government, AU unis |
| `terminalfour` | CMS fingerprint | UK/IE universities |
| `moderncampus` | CMS fingerprint | US Omni CMS unis |
| `courseleaf` | CMS fingerprint | US course catalog system |
| `sitecore` | CMS fingerprint | Large enterprise sites |
| `sharepoint` | CMS fingerprint | MS-stack institutions |
| `joomla` | CMS fingerprint | Older university sites |
| `silverstripe` | CMS fingerprint | NZ/AU universities |
| `browser` | Strategy fallback | JS SPAs without detected CMS |
| `static_html` | Strategy fallback | Plain server-rendered sites |

---

## KPIs

| KPI | Target | Component |
|---|---|---|
| First-scrape completeness | ≥ 85% | auto_config + patterns |
| Universities requiring YAML | < 5% | cascade + repair |
| Gemini calls per course | Near zero | skip gate + patterns |
| Repair reuse rate | Increasing monthly | pattern_store flywheel |

---

## Files by Stage

| Stage | Primary files |
|---|---|
| 1 Probe | `site_probe.py`, `library_strategy.py` |
| 4A CMS fingerprint | `site_probe._detect_cms_platform`, `auto_config_generator._derive_platform_type` |
| 5 Auto-config + AI rules | `auto_config_generator.py`, `ai_extractor_gen.py` |
| 3+9 Pattern lookup/promote | `pattern_store.py`, `scraper_patterns` table |
| 6 Scrape + extract | `orchestrator.py`, `extract_course.py`, `per_course_browser.py` |
| 6 Generic API routing | `generic_search_api.py`, `orchestrator.py` |
| 6b PDF discovery | `pdf_link_discoverer.py` — scores + low-value filter |
| 6b PDF classification | `pdf_classifier.py` — 8 categories + low-value gate + Gemini fallback |
| 6b Entry req extraction | `entry_req_extractor.py` — ATAR/GPA/degree/work_exp/portfolio |
| 6b PDF pipeline | `pipelines/university_pdfs.py`, `pipelines/single_course.py` |
| 6b PDF quality gate | `orchestrator.py` (P6·QI block, post-loop backfill) |
| 7 Quality report | `quality_intelligence.py`, `scrape.py:get_university_quality_report` |
| 7b Quality action dispatcher | `quality_action_dispatcher.py`, `scrape_tasks.run_quality_actions` |
| 8 CASCADE | `orchestrator.py` (end of `run_scrape`), `scrape_tasks.py` |
| Frontend onboarding | `universities.tsx` ("Add by URL" modal) |
| 9 Verification | `verification_engine.py`, `field_verification_results` table |

---

## Phase 9 — Verification & Confidence Engine

After every course is staged, the verification engine cross-checks the
`scraped_field_evidence` rows to produce a per-field confidence score based on
**source agreement**, not just presence.

### Source weights

| Source type | Weight | Extraction methods |
|---|---|---|
| html | 30 | regex, CSS, heuristic, direct |
| pdf | 30 | uni_pdf:*, pdf:*, cricos_match |
| api | 30 | searchstax, json_api, solr |
| pattern | 5 | sibling_cache, approved_row, pattern |
| ai | 5 | gemini, ai_fallback, ai_primary |

### Confidence formula

```
score = Σ weight(source) for each source whose value matches consensus
If any source CONFLICTS: score = min(score // 2, 35), status = "conflict"

Status:
  verified       ≥ 85
  likely_correct 60–84
  needs_review   < 60 (no conflict)
  conflict       any source disagrees (score capped at 35)
```

### Auto-publish gate (Phase 9 addition)

`auto_publish.should_auto_publish()` now checks `avg_verification_confidence`:
- `None` (engine not run yet) → does NOT block
- `< 85` → blocks with reason "Phase 9: Avg verification confidence N% < 85%"

### New tables / columns

| Object | Purpose |
|---|---|
| `field_verification_results` | Per-field verification outcomes; UNIQUE on (sc_id, field) |
| `scraped_courses.avg_verification_confidence` | Pre-computed avg for fast querying |

### API endpoints

```
GET /api/verification/course/{sc_id}           — per-field detail
GET /api/verification/university/{uni_id}/summary — uni-level summary
GET /api/verification/dashboard                — fleet-wide metrics
```

### Frontend

`university-detail.tsx` shows a "Verification Intelligence" card (sky-blue,
Phase 9 badge) when `total_fields_verified > 0`.  Displays avg confidence,
status breakdown bar, and top conflict fields.

---

## Session plan closeout (2026-05-30)

| Task | Status | Notes |
|---|---|---|
| T001: _library_situation in auto_config | ✅ Done | auto_config_generator.py line 99 |
| T002: CASCADE 70% + strategy exclusion | ✅ Done | orchestrator.py:2835 + exclude_strategies |
| T003: Generic search API routing | ✅ Done | orchestrator.py:824-860 + generic_search_api.py |
| T004: Frontend Add by URL | ✅ Done | universities.tsx + /api/universities/add-by-url |
| T005: Architecture doc | ✅ Done | this file (backend-py/docs/autonomous_pipeline.md) |
| Phase 9: Verification Engine | ✅ Done | verification_engine.py + migration 025 + UI |
