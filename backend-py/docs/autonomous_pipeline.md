# Autonomous Scraping Pipeline — Architecture

> **Goal**: Any client enters a university website URL. The system probes it,
> selects the right strategy and library stack, generates config, runs the
> scrape, validates quality, and self-heals — with zero YAML or manual
> configuration required.

---

## Pipeline Stages

```
URL entered
    │
    ▼
┌─────────────────────────────────────────┐
│  Stage 1 — Site Probe                   │
│  site_probe.probe_site()                │
│                                         │
│  Detects:                               │
│  • Cloudflare / WAF block               │
│  • JS SPA (React / Vue / Angular)       │
│  • Hidden search APIs                   │
│    (SearchStax, Algolia, Coveo …)       │
│  • Sitemap + course URL count           │
│  • Wayback archive availability         │
│                                         │
│  Outputs: SiteProfile                   │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Stage 2 — Strategy Selection           │
│  site_probe._select_strategy()          │
│                                         │
│  Picks from escalation ladder:          │
│    search_api → sitemap_first →         │
│    static_html → wayback →              │
│    browser → proxy → blocked            │
│                                         │
│  Confidence score 0.0 – 1.0            │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Stage 3 — Library Stack Selection      │
│  library_strategy.recommend_library_stack()  │
│                                         │
│  Maps site signals → Python library     │
│  recommendation:                        │
│    fetch_library, parser, fallback,     │
│    antibot, data_cleaning, reason       │
│                                         │
│  Situations:                            │
│    hidden_api, cloudflare_stealth,      │
│    browser_automation, large_structured,│
│    sitemap_first, static_html,          │
│    wayback_archive, blocked             │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Stage 4 — Auto Config Generation       │
│  auto_config_generator.generate_config()│
│                                         │
│  Heuristic rules + Gemini refinement:   │
│  • URL allow/block patterns             │
│  • Fee page / IELTS page hints          │
│  • Search API provider wiring           │
│  • Currency, stealth flags              │
│                                         │
│  Written to: universities.scrape_config │
│    ["auto_config"]                      │
│                                         │
│  Key fields persisted:                  │
│    _strategy, _library_situation,       │
│    _api_provider, _api_endpoint_hint,   │
│    _probe_summary                       │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Stage 5 — Scrape Execution             │
│  orchestrator.run_scrape()              │
│                                         │
│  Discovery routing (priority order):   │
│  1. YAML searchstax block (emergency   │
│     per-uni override, e.g. HUD)        │
│  2. auto_config._api_provider          │  ← NEW (generic_search_api.py)
│     → generic_search_api.fetch_generic_api_links()
│  3. BFS / sitemap / browser / Wayback  │
│     (standard discovery tiers)         │
│                                         │
│  Extraction: single_course.extract()   │
│  Staging:    stage_scraped_course()    │
│  Completeness computed per-course      │
│    (13 review fields)                  │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│  Stage 6 — Quality Validation           │
│                                         │
│  Per-job metrics after scrape:          │
│  • staged_n   = count of staged rows   │
│  • avg_completeness over staged rows   │
│                                         │
│  Auto-publish gate: ≥ 85% completeness │
│  Cascade gate:      staged < 5          │
│                     OR avg < 70%       │  ← bumped from 50%
└────────────────┬────────────────────────┘
                 │
          ┌──────┴───────┐
          │              │
      ≥ 70%            < 70%
       good            poor
          │              │
          ▼              ▼
    Done / auto-   ┌─────────────────────────────────┐
    publish queue  │  Stage 7 — Self-Heal (CASCADE)  │
                   │  orchestrator → probe_and_configure
                   │  triggered_by="cascade"          │
                   │  exclude_strategies=[failed_strat]│
                   │                                   │
                   │  probe_and_configure:            │
                   │  • Re-probes site                │
                   │  • If same strategy → advance    │
                   │    to next ladder rung           │
                   │  • Generates new auto_config     │
                   │  • Queues a new scrape job       │
                   │    automatically                 │
                   └────────────┬────────────────────┘
                                │
                                └──→ back to Stage 5
```

---

## Key File Map

| Concern | File |
|---|---|
| Site probe | `backend-py/app/services/scraper/site_probe.py` |
| Strategy selection | `site_probe._select_strategy()` |
| Library stack advisor | `backend-py/app/services/scraper/library_strategy.py` |
| Auto config generator | `backend-py/app/services/scraper/auto_config_generator.py` |
| Generic search API provider | `backend-py/app/services/scraper/generic_search_api.py` |
| HUD SearchStax (specific) | `backend-py/app/services/scraper/searchstax_hud.py` |
| Orchestrator + CASCADE | `backend-py/app/services/scraper/orchestrator.py` |
| Probe Celery task | `backend-py/app/tasks/scrape_tasks.probe_and_configure` |
| Per-uni YAML (emergency override) | `backend-py/scraper_config/unis/<slug>.yaml` |
| Frontend probe card | `artifacts/university-portal/src/pages/university-detail.tsx` |
| Frontend add-by-URL | `artifacts/university-portal/src/pages/universities.tsx` |

---

## Connection Points

### Probe → Library Stack → auto_config
```
probe_site() → recommend_library_stack() → profile.library_stack
                                              ↓
                         auto_config_generator._base_config()
                           writes _library_situation to auto_config
                           writes _api_provider if search API detected
                           writes _api_endpoint_hint
```

### auto_config → Scrape routing
```
orchestrator.run_scrape()
  reads uni_scrape_config["auto_config"]
    _api_provider → generic_search_api.fetch_generic_api_links()
    use_stealth_browser → stealth browser pool
    use_wayback → Wayback discovery tier
    always_sitemap_supplement → sitemap BFS supplement
    allow_url_patterns → BFS URL filter
```

### Scrape → CASCADE → re-probe
```
orchestrator.run_scrape() completes
  computes avg_completeness
  if avg < 70% and no per-uni YAML:
    probe_and_configure.delay(
      uni_id,
      triggered_by="cascade",
      exclude_strategies=[_ac_strategy]   ← strategy that just failed
    )
    → re-probe skips failed strategy
    → picks next ladder rung
    → generates new auto_config
    → queues new scrape job automatically
```

---

## YAML as Emergency Override

Per-university YAML files (`scraper_config/unis/<slug>.yaml`) exist for
edge-cases that the autonomous system cannot yet handle automatically:

- Custom Solr configurations requiring a specific token + field mapping
- Per-host URL query parameter injection (e.g. `?international=true`)
- Universities where BFS page budget needs manual tuning

**Rule**: YAML overrides win over auto_config at every layer (loader deep-merge
order: defaults → auto_config → per-uni YAML). The CASCADE self-heal skips
universities that have a per-uni YAML file to avoid overwriting hand-tuned
settings.

When a YAML file is no longer needed (autonomous system handles the site well),
delete it and re-run the probe — the system will re-configure itself.

---

## Adding a New University (Zero-Config Flow)

1. Click **Add University** in the portal, enter the name and URL, check
   **Auto-configure & scrape immediately**.
2. System creates the university record, immediately calls
   `POST /api/universities/{id}/probe`.
3. `probe_and_configure` Celery task runs:
   - Probes the site (stages 1–3 above)
   - Generates auto_config (stage 4)
   - If `triggered_by="cascade"`: queues a scrape job automatically
4. For manual trigger: operator clicks **Start Scrape** on the university detail
   page (the probe result card shows the recommended strategy).
5. Scrape runs with auto_config (stage 5).
6. If completeness < 70%: CASCADE fires, re-probes with a different strategy,
   auto-queues another scrape (stage 7).
7. If completeness ≥ 70% but < 85%: courses land in `review` status for human
   approval.
8. If completeness ≥ 85%: courses auto-published immediately.

---

## Anti-Patterns to Avoid

| Don't | Do instead |
|---|---|
| Writing a per-uni YAML for a new university | Run the probe and let auto_config handle it |
| Hardcoding provider logic for a specific university | Generalise in `generic_search_api.py` |
| Lowering the CASCADE threshold below 70% | The 70% threshold is intentional — 50% was too permissive |
| Setting `SHADOW_MODE_UNI_IDS` without a plan | Only use for migrations with a defined cutover criterion |
| Committing tokens / API keys to YAML files | Always use `token_env: ENV_VAR_NAME` in YAML |
