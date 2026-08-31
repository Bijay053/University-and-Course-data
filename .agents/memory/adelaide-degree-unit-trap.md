---
name: Adelaide University degree vs unit URL trap
description: Adelaide has two URL spaces that look similar; /study/degrees/ are programs; /study/courses/ are individual units — must be blocked.
---

## The rule
`adelaide.edu.au` has two URL namespaces with confusing naming:

| URL pattern | Count in sitemap | Meaning |
|---|---|---|
| `/study/degrees/<slug>/` | 521 | Degree-level **programs** (what we want) |
| `/study/degrees/online/<slug>/` | 51 | Online degree programs (also want) |
| `/study/courses/<code>/` | 5,523 | Individual **unit** pages like `acct-1001` — NOT programs |

The default BFS finds only 5 courses because `/study/degrees/` listing page loads degree links via JavaScript (static HTML has essentially no degree hrefs). The 1 MB sitemap at `https://adelaide.edu.au/sitemap.xml` lists all 571 degree pages.

**Why:** The BFS starts from the homepage, follows nav links, and never reaches individual degree pages through static HTML alone. `allow_url_patterns: [/study/degrees/]` is not enough if the listing page itself has no links to follow.

## How to apply
- `always_sitemap_supplement: true` + explicit `sitemap_url: https://adelaide.edu.au/sitemap.xml`
- `allow_url_patterns: [/study/degrees/]`
- `block_url_patterns: [/study/courses/, /study/degrees/compare-degrees/, /study/degrees/2026/, /study/degrees/2027/]`
- `bfs_page_budget: 2` (minimal; sitemap does the heavy lifting)
- Expected candidates from sitemap: ~571

## Data available in static HTML (no browser rendering needed)
- Fee: `$XX,XXX` in `.degree-details-content-section-subtitle > span` (tooltip confirms "Published fees are for international students starting in 2026")
- IELTS: `IELTS Overall X.X` in page text
- Intake: pipe-separated month string e.g. `February|July`
- Duration: `X year(s) full-time`
- Study mode: `On campus` / `online`

No Cloudflare detected; pages are 400–500 KB static HTML.

## Dormant domestic-only modal

Every Adelaide degree page embeds a reusable `dom-modal-exclusive` dialog whose title says the degree is only available to Australian students. The dialog also exists on international-eligible degrees with international fees and CRICOS data, so its dormant subtree is page chrome and must be excluded from domestic-only matching.

**Why:** Treating that hidden dialog as a hard course-level signal rejected 534 of 560 discovered pages in one run, including Bachelor of Arts, Bachelor of Nursing, and international IT degrees.

**How to apply:** Remove only Adelaide's `dialog[data-modal-opener="dom-modal-exclusive"]` subtree before both static and rendered hard-marker checks. Continue honoring explicit domestic-only statements elsewhere on the page. Use the page-level `studentType` metadata as the authoritative availability signal: `Domestic` alone is ineligible, while `Domestic|International` is eligible. This also prevents a domestic page fee from being promoted to `international_fee` when the International URL redirects back to `/dom/`.
