---
name: Slug derivation for non-www subdomains
description: _hostname_to_slug only stripped www. originally; any other subdomain (study., international., courses.) became the slug, so the wrong YAML was loaded.
---

## Rule
`_hostname_to_slug` must strip ALL generic subdomain prefixes, not just `www.`.
The fix adds `_GENERIC_SUBDOMAINS` frozenset and loops while `parts[0]` is in it.

**Why:** CSU's scrape URL is `study.csu.edu.au`. Old code: `study.csu.edu.au` → slug `study` → no YAML found → defaults loaded (online_only.enabled=True, scrape_do_fallback=False). Both the guards.py online_only fix AND the scrape.do fallback were silently dead.

**How to apply:** When onboarding a university whose scrape_url uses a subdomain other than `www.` (study., international., courses., handbook., programs., admissions., etc.), verify `_hostname_to_slug(hostname)` returns the institution label, not the subdomain label. Add new generic subdomains to `_GENERIC_SUBDOMAINS` in `config/loader.py` as needed.

**Fixed in:** `backend-py/app/services/scraper/config/loader.py`, `_hostname_to_slug` and `_GENERIC_SUBDOMAINS` constant.
