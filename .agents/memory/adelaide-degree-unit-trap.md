---
name: Adelaide University degree-page traps
description: Adelaide's degree/unit URL boundary and conjunctive domestic-only eligibility signals.
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

**How to apply:** Remove only Adelaide's dormant shared dialog subtree before broad hard-marker checks. Continue honoring explicit domestic-only statements elsewhere on the page.

## Conjunctive Adelaide eligibility rule

`studentType=Domestic` without `International` is ineligible. Dual-audience metadata is not conclusive: reject `Domestic|International` only when paired with Adelaide's exclusive audience component, its hydrated exclusive selector, or an open domestic-exclusion dialog. Do not reject an exclusive component paired with `studentType=International` alone.

**Why:** A live catalogue inventory found three metadata-inconsistent domestic-only programmes with dual-audience metadata plus the exclusive state, while seven legitimate international-only programmes reuse the exclusive component. Browser checks confirmed the three inconsistent pages activate the Australian-only dialog after selecting International.

**How to apply:** Require the metadata/state conjunction in both static and rendered HTML. Keep the shared dormant dialog suppressed, and do not force generic per-course browser rendering because the static exclusive-state signal catches these pages before browser or AI recovery.

## Online catalogue authority

Adelaide's `/study/degrees/online/` route is an institution-owned online-only signal. A non-online alias is also online-only when the course-owned metadata pairs `courseMode` of `Online`, `Online only`, or `100% online` with an all-virtual `location`.

**Why:** Adelaide degree pages share navigation links for both online study and physical campuses. Generic page-wide extraction misread that chrome as mixed delivery, converting true `100% online` degrees to `Blended` with a synthetic Adelaide location.

**How to apply:** Reject these routes and paired metadata before generic mode/location extraction, browser recovery, or AI. Do not match bare page text or shared navigation; ordinary campus degree pages also mention online study.
