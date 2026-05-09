# University Onboarding Patterns

Living document. Updated after every 2 unis are processed in Week 4 (Prompt 5).
Goal: each new uni after the top 10 takes 30 minutes instead of 2 hours.

---

## Site platforms encountered

_Fill in as each uni is processed._

| University | Platform | Discovery method | Notes |
|------------|----------|------------------|-------|
| Monash | TBD | TBD | TBD |
| Melbourne | TBD | TBD | TBD |
| Sydney | TBD | TBD | TBD |
| ... | | | |

---

## Common gotchas by category

### Discovery

- **/study/postgraduate vs /courses/postgraduate vs /degrees/masters** — observed in ~40% of
  cases.  Add `discovery.extra_discovery_seeds` in per-uni YAML.
- **Cloudflare-protected sites** — set `skip_static_fetch: true`; reduces scrape time ~50%.
- **JS-rendered SPAs** (CSU pattern) — set `discovery.always_sitemap_supplement: true`.

### Fee extraction

- **Per-credit-point vs per-year fees** — confirm `extraction.fees.credit_points_per_unit`
  is set in the per-uni YAML for unis that publish per-credit-point only.
- **Domestic vs International tabs** — confirm International tab promotion is firing
  (Week 1 fix).

### Location extraction

- **Unit-level study centres listed alongside course locations** — needs course-level
  CSS selector override (per-uni YAML key TBD).
- **"Online" listed as both location AND study mode** — handled separately by
  `extraction.text_cleaning.location.strip_patterns`.

### IELTS extraction

- Confirm Week 2 Prompt 5 (skip central propagation) is active for unis with
  central English page + per-course overrides.
- Pathway / preparatory programs need lower sanity floors — Week 2 Prompt 6.

### CRICOS extraction (AU only)

- Live coverage is currently 0% for CSU / UOW / VIT (see `v_cricos_coverage_au`).
- Diagnostic: `python scripts/cricos_coverage_diagnostic.py --uni-id N`.
- If "page mentions CRICOS" > 0 but "extractor matched" = 0, the regex needs a
  pattern for that uni's specific format.  Add to `extractors/cricos_code.py`
  CRICOS_LABEL_PATTERNS only after 23-uni regression sweep passes.

---

## Reusable per-uni YAML templates

### Template: WAF / Cloudflare-heavy site

```yaml
discovery:
  always_sitemap_supplement: true
extraction:
  filters:
    domestic_only:
      enabled: false   # Many international course pages get false-flagged
```

### Template: SPA with sitemap (CSU pattern)

```yaml
discovery:
  always_sitemap_supplement: true
extraction:
  filters:
    domestic_only:
      enabled: true
```

### Template: Standard server-rendered HTML (low risk)

```yaml
discovery: {}
extraction:
  filters:
    domestic_only:
      enabled: false
```

---

## Unis processed (Week 4)

_Update after each scrape attempt._

| Uni | Status | Date | YAML changes | Issue summary |
|-----|--------|------|--------------|---------------|
| Monash | NOT_STARTED | — | — | — |
| Melbourne | NOT_STARTED | — | — | — |
| Sydney | NOT_STARTED | — | — | — |
| UNSW | NOT_STARTED | — | — | — |
| UQ | NOT_STARTED | — | — | — |
| RMIT | NOT_STARTED | — | — | — |
| Deakin | NOT_STARTED | — | — | — |
| UTS | NOT_STARTED | — | — | — |
| ANU | NOT_STARTED | — | — | — |
| UWA | NOT_STARTED | — | — | — |
