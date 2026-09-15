---
name: MQ Funnelback authority + CF bypass
description: Macquarie requires rendered Funnelback discovery plus structured page-data enrichment; bare-link fallbacks expose domestic defaults.
---

## Source authority

The international Funnelback profile is not an exhaustive authority for
research degrees: check the separate Graduate Research Academy PhD and MPhil
pages before revising the catalogue baseline.

**Why:** The official research site advertises international full-time PhD and
MPhil routes that were absent from the profile-backed staged catalogue.
Counting taught doctorates or Master of Research courses does not establish
coverage of these qualifications.

**How to apply:** Reconcile current-route identities, year aliases, dual
public/page-data not-found confirmations, and staging eligibility before
comparing counts. Pre-enrichment index rows are not validated current degrees;
neither a higher raw count nor a clean enrichment pass proves completeness.

Dual origin-not-found confirmation is a per-run exclusion, not proof of
permanent retirement.

**Why:** Two MQ courses returned on the next same-release scrape after both
their public and page-data routes had reported origin 404 in the prior run.

**How to apply:** Preserve live records and avoid deriving a lower permanent
catalogue floor solely from one run's dual-404 exclusions.

Funnelback can include postgraduate/undergraduate specialisation subpaths and
retired degree links. Classify exact subpath segments and confirm both public
detail and page-data origin-not-found before excluding stale links.

**Why:** A fee-coverage failure repeated unchanged after retries because
non-degree specialisations inflated the denominator and dead endpoints
returned 404, not transient failures. International annual fee strings also
use thousands separators, so numeric conversion must accept valid grouping.

**How to apply:** Preserve the coverage thresholds, retain blocked/unknown
courses in their denominator, and never use discovery exclusions to delete
existing live catalogue records. Accept the fully validated rich-provider
catalogue without a second fixed course-count gate: removing stale/non-degree
links legitimately lowers its size, and bare-link supplementation reintroduces
the domestic-default data the rich provider avoids.

Use the international Funnelback catalogue only through the rich MQ provider that retains structured page-data fees, English scores, duration, and evidence. A generic provider that returns only URLs is unsafe because downstream detail pages render the domestic audience by default.

**Why:** A bare-link production run found the full catalogue but staged almost every degree without international fees, while also admitting majors and specialisations as courses. Link-count completeness did not imply data completeness.

**How to apply:** Give the rich provider precedence over caches and generated API recipes. Reject majors and specialisations. Fail the scrape rather than supplementing with domestic HTML when pagination, structured-page coverage, or fee coverage is materially incomplete.

Rich provider links short-circuit ordinary per-course extraction, so every
authoritative page-data field must be copied into the provider payload itself.

**Why:** rendered page-data recovered almost the full catalogue, but omitting
`study_level` and `course_duration_in_years.label` from the provider handoff
staged an apparently successful run with duration missing on every row.

**How to apply:** regression-test the final rich-link payload, not only JSON
parsing or recovery counts. Funnelback values retain priority when present;
page-data fills fields Funnelback omits.

## Transport

Current research-certificate eligibility should come from the official
year-specific handbook's structured offering record, not default admissions
HTML or absence from Funnelback.

**Why:** The Arts and Science/Engineering certificates had published handbook
records but explicitly false international eligibility and non-offered
sessions. A published qualification is not necessarily a current offering;
neither default domestic HTML nor an empty CRICOS field alone proves this.

**How to apply:** Recheck the current year's international and session-offering
fields together. Record ineligibility for that year without calling the course
permanently domestic-only or deleting live records.

Every part of `www.mq.edu.au` and both Funnelback endpoints are behind Cloudflare Enterprise.
- Direct httpx / requests / curl_cffi / Scrapy → CF 403
- scrape.do render=false (static proxy) → 502 ROTATION_FAILED for all MQ URLs
- scrape.do render=true (real Chrome) → ✅ works for both homepage and Funnelback JSON endpoint

**Funnelback endpoint:**
`https://mqu-search.funnelback.squiz.cloud/s/search.json?collection=mqu~sp-courses&profile=international&query=!padrenull`

**Pagination:** 1-based (`start_rank=1` for first result). Server caps at 200 results per request.
- Page 1: `start_rank=1&num_ranks=200` → 200 results
- Page 2: `start_rank=201&num_ranks=200` → ~168 results
- Total: ~368 courses (fullyMatching=368 per resultsSummary)

**Chrome wraps JSON in `<pre>` tag:** When scrape.do render=true opens a JSON URL,
Chrome displays it as `<html><body><pre>{json}</pre></body></html>` and may append
an empty `json-formatter-container` sibling after the `<pre>`. Rendered JSON
fetches must accept that exact browser-owned wrapper while rejecting arbitrary
HTML around otherwise valid JSON.

**Reference Python spider** (`macquarie_university_au_1786509801112.py`) uses `websearch.mq.edu.au`
and works from non-datacenter IPs (local machines). From Replit servers that endpoint is also CF-blocked.

**Discovery of alternate endpoints tested (all fail from server):**
- `websearch.mq.edu.au` → CF 403 (httpx, requests, curl_cffi, Scrapy all blocked)
- `mqu-search.funnelback.squiz.cloud` → CF 403 direct; ROTATION_FAILED via scrape.do static
- `www.mq.edu.au` sitemap / page-data.json → CF 403 direct; ROTATION_FAILED via scrape.do static
- Wayback CDX → only 73 current course URLs (not enough)

The rendered Funnelback path gets the catalogue in two API calls and avoids the old BFS limit, but it is acceptable only when paired with structured international enrichment and explicit coverage gates.
