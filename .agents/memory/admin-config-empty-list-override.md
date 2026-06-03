---
name: admin_config empty list overrides YAML patterns
description: Empty lists [] in DB admin_config.discovery silently clear YAML-defined allow_url_patterns/block_url_patterns; fixed in _deep_merge and by DB cleanup.
---

## Rule

Empty lists `[]` in `admin_config.discovery` (written by the Scrape Fix Agent UI) silently overwrote non-empty lists from the per-uni YAML file during `_deep_merge`.

## Why

`_deep_merge` used to blindly assign `result[key] = val` for all keys including `val = []`.
When the operator's browser submitted a fix form with empty allow/block fields, the UI wrote `{"discovery": {"allow_url_patterns": [], "block_url_patterns": []}}` to DB.
On the next scrape, this cleared the YAML's `allow_url_patterns` → `compiled_allow = []` → `_passes()` returned `True` for every URL → filter disabled.

## Fix (applied)

1. **`_deep_merge` guard** (`loader.py`): for list-type fields in `_LIST_NO_CLEAR_KEYS` (`allow_url_patterns`, `block_url_patterns`, `must_contain`, `seed_urls`, `block_nav_patterns`, `fallback_subdomains`), an empty list `[]` or `None` in the override is skipped — it does NOT clear an existing non-empty list in base.

2. **DB cleanup pattern**: if a university's YAML allow/block patterns aren't filtering, check:
   ```sql
   SELECT scrape_config->'admin_config'->'discovery' FROM universities WHERE id = <N>;
   ```
   If it has `{"allow_url_patterns": [], "block_url_patterns": []}`, remove those keys:
   ```sql
   UPDATE universities
   SET scrape_config = jsonb_set(
     scrape_config,
     '{admin_config,discovery}',
     (scrape_config->'admin_config'->'discovery') - 'allow_url_patterns' - 'block_url_patterns'
   )
   WHERE id = <N>;
   ```

## How to apply

Before diagnosing "why is allow_url_patterns not filtering?" — always check DB admin_config first.
The loader verify call `get_config_for_host(db_scrape_config={})` (empty DB config) will ALWAYS show YAML patterns; call it with `db_scrape_config=dict(u.scrape_config or {})` (real DB) to see if admin_config is overriding.
