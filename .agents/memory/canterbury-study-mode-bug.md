---
name: Canterbury UC staging bug & study_mode fix
description: Two-part bug causing Canterbury (ID 1750) to stage only 49/220+ courses; root cause, fixes, and study_mode suppression pattern.
---

## Root cause (2-part bug)

**Bug 1 — admin_config DB override**: `universities.scrape_config->'admin_config'->'extraction'->'filters'->'online_only'->>'enabled' = 'true'` was set in the DB. Per `loader.py` the priority chain (lowest→highest) is: pydantic defaults → defaults.yaml → DB uniPages → auto_config → per-uni YAML → **admin_config** (HIGHEST). So the admin_config `online_only:true` silently overrode the YAML's `online_only: enabled: false`. All bachelor pages had study_mode=Online (false positive from nav) → rejected as online-only.

**Bug 2 — study_mode nav false positive**: Canterbury's nav bar contains "UC Online Staff" on EVERY page. The nav is rendered inside a `<div>`/`<header>` element, NOT `<nav>`, so `_NOISE_BLOCK_RE` (which strips `<nav|form|select|footer|aside>`) does NOT remove it. The bare `\bonline\b` fallback pattern (confidence 0.5) fires on "UC Online Staff" and the structural protection in `single_course.py` then BLOCKS Gemini from overriding study_mode set by the rule extractor.

## Fixes applied

**Fix 1 (DB)**: Remove admin_config online_only override:
```sql
UPDATE universities
SET scrape_config = jsonb_set(scrape_config, '{admin_config,extraction,filters}',
    (scrape_config->'admin_config'->'extraction'->'filters') - 'online_only')
WHERE id = 1750;
```

**Fix 2 (YAML)**: Added `"UC Online"` to `global_substring_blocklist` in `canterbury.yaml` (strips it from extracted field VALUES post-extraction — note: does NOT prevent mode extractor from firing on raw HTML).

**Fix 3 (study_mode.py)**: Added `www.canterbury.ac.nz` and `canterbury.ac.nz` to `_STUDY_MODE_RULE_SUPPRESSED_HOSTS` frozenset. This makes the rule extractor return `[]` for Canterbury, letting Gemini classify study_mode freely from full page context.

## Results

- Before: 49 staged courses (bachelors all rejected as online-only)
- After: 217 staged courses, avg completeness 91.1%
- Study mode: 171 On Campus (79%), 26 Blended (12%), 18 Online (8%) — correct

## Generalizable lessons

1. **admin_config is the HIGHEST priority** — always check `scrape_config->'admin_config'` when YAML settings appear to be ignored. DB admin_config wins over everything including YAML.
2. **`_STUDY_MODE_RULE_SUPPRESSED_HOSTS` pattern**: When a university's nav/boilerplate contains "online" in a non-`<nav>` HTML element, add the host to this frozenset in `study_mode.py` so Gemini classifies freely.
3. **`global_substring_blocklist` acts on PAYLOAD VALUES** (post-extraction), not raw HTML. It cannot prevent mode extractor from firing on nav text.

## 19 DQF courses (expected, not a bug)

Canterbury's central fee page bucket match covers Bachelors/Masters/GradDip/GradCert but NOT Certificate/Diploma level. 16 certs/diplomas have no fee → `missing_international_fee` critical (since `has_central_fee_page=False` for those rows). 3 have fee=1166 NZD ≈ 1049 AUD which is below `crit_min=5000` AUD for Certificate tier. Requires human review — legitimate data gap, not a pipeline bug.
