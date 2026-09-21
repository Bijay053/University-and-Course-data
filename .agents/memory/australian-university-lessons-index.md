# Australian university discovery and extraction

## Catalogue discovery and transport

- [UniSQ SSR and location drift](unisq-browser-timeout.md) — SSR beats Playwright; compare saved location evidence with fresh quick-facts before changing the parser for source drift.
- [UniSC sitemap + config identity](unisc-sitemap-discovery.md) — XMLsitemap supplies courses; production ID-specific YAML can shadow the verified recipe, so shared name cleanup must not depend on its aliases.
- [Bond University sitemap discovery](bond-sitemap-discovery.md) — generic_search_api gated by `if not links`; BFS fills it first; sitemap.xml index (5 child pages, 240 URLs) is the correct approach.
- [SCU HTML-comment hidden URLs](scu-html-comment-hidden.md) — base course URLs inside an HTML comment; _LinkExtractor finds 0; force_candidate_url_patterns is useless here; fix = allow year-versioned URLs through (remove /2027/ block, prefer 2027).
- [UNE Wayback CDX discovery](une-wayback-discovery.md) — listing page is React SPA (0 links even with Scrape.do render); no sitemap; Wayback CDX with /study/courses/* prefix is the only discovery mechanism.
- [MQ Scrape.do render false fix](mq-scrape-do-render-lesson.md) — scrape_do_render+skip_fallbacks caused 41/127 timeouts; stealth browser is correct for MQ; degree_level_defaults fills fees.
- [CSU discovery fix](csu-discovery-fix.md) — CF Enterprise; 3 YAML knobs (skip_browser_discovery+sitemap_url+use_wayback) skip 160s of dead probes; CDX→329 course URLs.
- [Griffith program API authority](griffith-program-api.md) — degree pages are Vue shells; use v3 program API, with Funnelback metadata fallback for retired 404 records.
- [CQU sitemap-only discovery](cqu-sitemap-only-discovery.md) — rendered /courses links only to broad navigation; skip BFS and use the static-proxy sitemap directly.

## Course fields and page structure

- [UniSC fee and English authority](unisc-fee-english-authority.md) — exclusive international fees; Table 1 by level; Table 2 only by exact program title.
- [UOW session-fee table](uow-session-fee.md) — UOW’s adjacent Course fee is a full-programme total; select and retain the Session fee column instead.
- [UOW IELTS skill table](uow-ielts-skill-table.md) — course pages put band labels in the header before the IELTS row; parse DOM columns or flattened prose drops every sub-band.
- [Flinders AEM page compaction](flinders-aem-compaction.md) — course pages are mostly AEM chrome; retain metadata, title shell and fast facts before generic extraction.
- [CQU English field drift](cqu-english-field-drift.md) — current course English may live in AIMS requisite conditions while dedicated English/schema fields are empty; TOEFL can list PBT before iBT.
- [CSU duration and offering-year authority](csu-duration-offering-year.md) — use minimum/standard full-time duration and latest-year FPOS modes; embedded maxima and mixed years can corrupt current values.

## Audience and delivery authority

- [Adelaide degree-page traps](adelaide-degree-unit-trap.md) — dual-audience needs exclusive state to reject; `/degrees/online/` and paired mode/location metadata mean online-only.
- [UOW international study load](uow-international-study-load.md) — “N years, or part-time equivalent” means the primary full-time route; part-time-only courses are ineligible.
- [UTS audience-state fee override](uts-audience-fee-override.md) — static UTS pages look complete but show Domestic fees; required browser actions must override amount and fee metadata together.
- [CDU audience-scoped course fields](cdu-audience-scoped-fields.md) — fees and locations must come from the current course’s international DOM blocks, never domestic siblings or related-course cards.
- [WSU international catalogue authority](wsu-algolia-authority.md) — detail pages default to domestic; Algolia owns international fees/durations, while official English profiles require named exceptions.