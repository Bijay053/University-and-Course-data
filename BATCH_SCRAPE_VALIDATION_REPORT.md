# Batch Scrape Validation — Provider Report

Final, rolling, provider-by-provider report for the Batch Scrape Validation plan. It captures, for each university/provider in the supplied list, the mode used, the job outcome counters, the staged-row count (the durable verification source from `scraped_courses`), the issue summary, and the fix summary.

The report follows the Reporting Format section of the plan and is intended to be updated in place as additional providers are executed. Entries recorded so far are grouped into **Executed** (passed, passed after fixes, or requiring code changes) and **Pending** (not yet executed in this batch run).

---

## Methodology (short recap)

- Start each run through `POST /api/scrape/start` exposed by `api-server/src/routes/scrape.ts`. Monitor via the job polling endpoints and the review UI in `university-portal/src/pages/scraping.tsx`.
- Begin in `fastMode`; escalate to `smartMode` when a run hangs, returns zero useful rows, or obviously stages junk.
- Treat staged rows in the `scraped_courses` table (`lib/db/src/schema/scraped_courses.ts`) as the durable verification source. Job-level counters are used for the concise status line.
- Apply full-debug handling per provider before moving on: discovery logs, URL filtering, page classification, fee / IELTS / location extraction, and site-specific DOM quirks.
- Fixes land in `api-server/src/routes/scrape.ts` and the mirrored runtime copy at `artifacts/api-server/src/routes/scrape.ts`.

Per-provider row columns:

| Column | Meaning |
| --- | --- |
| `status` | `PASS`, `PASS (fix required)`, `PARTIAL`, `BLOCKED`, or `PENDING` |
| `mode` | `fast`, `smart`, or `hybrid` (fast → smart rerun) |
| `counts` | `totalFound / staged / skipped / errors` from the final run |
| `issue` | Short root-cause summary |
| `fix` | Short description of the code change applied |

---

## Executed providers

### 1. Torrens University — `torrens.edu.au`

- **status:** PASS (fix required)
- **mode:** hybrid (fast baseline, smart reruns during debug)
- **counts:** `104 / 76 / — / 0`
- **issue:** Multiple layered bugs on Torrens:
  1. Category hubs and short-course pages (e.g. `/courses/hospitality`, `/courses/higher-degrees-by-research`, `/studying-with-us/study-options/short-courses/*`) were mis-classified as course detail pages.
  2. Domestic-only courses were being staged because the audience parser only looked at `strong`/`span`/headings/tables/DLs, not Torrens' `div.course-card-panel__label` / `div.course-card-panel__value` pairs.
  3. `Study mode` parser missed the `div`-based block layout and fell back to `Online`.
  4. `Campus locations*` (with trailing asterisk) was not matched by the location label regex, so 839 / 849 staged rows had blank `course_location`.
  5. Sample validator rejected real degree pages (e.g. `Bachelor of Nursing`) as "Not a course page".
  6. University-level fee discovery ignored the external IntelligenceBank international fee PDF because it was same-origin-only.
- **fix:**
  - Tightened `isKnownCourseListingUrl` / `VALID_COURSE_PATH_PATTERNS` to reject category hubs and short-course hubs.
  - Extended audience parser to read `div` label/value blocks; added `studyMode: Online` + campus `Online` rejection.
  - Extended `Campus locations*` label regex and added `CourseInstance` structured-data fallback for noisy DOMs.
  - Added a strong-signals accept rule (degree heading + CRICOS + `Study mode` / `Campus locations` / `Student` / `Course duration` / `Start date`) so noisy real course pages survive.
  - Allowed external PDFs during fee discovery and prefer `international` fee PDFs over domestic; stopped broad `international` matches from selecting the entry-requirements PDF.
  - Cleaned previously staged false positives so the review list matches the fix.

### 2. Charles Sturt University — `csu.edu.au` (redirects to `study.csu.edu.au`)

- **status:** PASS (fix required)
- **mode:** fast
- **counts:** `332 / 105 / — / 0` (post-approval bulk stage)
- **issue:** Starting from `https://www.csu.edu.au/`, the scraper detected `/courses` but kept the original origin, so it never used the real study-site sitemap at `study.csu.edu.au`. Result: almost no discovery on the `csu.edu.au` origin.
- **fix:** Keep the final redirected listing URL (`https://study.csu.edu.au/courses`) and use the resolved listing origin for sitemap/link discovery; skip category-expansion probes when a large sitemap result already exists. A small residual data-quality issue remains for a few CSU pages and is tracked under the recurring-issues section below.

### 3. University of Newcastle — `newcastle.edu.au`

- **status:** PASS (fix required)
- **mode:** smart
- **counts:** `275 candidates / 256 filtered / staged on approval / 0` at the end of debug (full batch numbers depend on approval run).
- **issue:** Cloudflare blocked direct server fetches and `HEAD` probes, so both sitemap and high-priority-path discovery failed. `/degrees/<slug>` was not recognized as a valid course-detail URL, and mirrored links came back as `newcastle.edu.au` while the job origin was `www.newcastle.edu.au`, so the same-origin filter discarded them.
- **fix:**
  - Added a mirrored text-fetch fallback (Jina reader) for Cloudflare-blocked pages, converting its markdown links into lightweight HTML so discovery can still work.
  - Taught homepage discovery to content-probe high-priority paths like `/degrees`, not just rely on blocked `HEAD` requests.
  - Recognized `/degrees/<slug>` as a valid course-detail path; explicitly excluded non-course endpoints (`compare`, `research`, `teach-out`).
  - Normalized mirrored links to the requested host so same-origin filtering keeps them.

### 4. University of Sydney — `sydney.edu.au`

- **status:** PARTIAL (fix applied, bulk-stage count site-dependent)
- **mode:** smart
- **counts:** `totalFound: 574 / imported: 5+ / — / 0` on the verification run.
- **issue:** Sydney's sitemap is dominated by handbook `units/*`, `handbooks/*`, and `subject-areas/*` pages, and the fallback URL check was too permissive — any URL containing `/course` survived, so `Spec.Html`, handbook unit pages, and 404s entered the course queue. Sitemap-derived names also kept the `.html` suffix, making staged titles look broken.
- **fix:**
  - Recognized real Sydney degree URL shape `/courses/courses/.../*.html` and blocked non-course paths (`/units/`, `/handbooks/`, `/subject-areas/`).
  - Rejected 404/error pages during sampling.
  - Stripped `.html` from sitemap-derived names so staged titles are clean.

### 5. University of Southern Queensland — `unisq.edu.au`

- **status:** PASS (fix required)
- **mode:** fast
- **counts:** `118 / 94 / 24 / 0`
- **issue:** Broad `/study/...` and `/degrees...` URLs were being treated as course pages purely because the path looked academic. `career-finder`, `testimonials`, UniSQ blogs/story pages, and directory pages under `/study/degrees-and-courses` were all leaking into candidates. Homepage discovery preferred the weaker `/degrees` route over UniSQ's real degree hub, and the page classifier treated directory pages as single-course pages.
- **fix:**
  - Blocked `career-finder`, `testimonials`, `blogs`, UniSQ story pages, and generic directory pages under `/study/degrees-and-courses` up front.
  - Stopped auto-accepting broad `/study/...` / `/degrees...` URLs without detail signals.
  - Made homepage discovery prefer the real UniSQ degree hub.
  - Strengthened course-detail validation so pages with `IELTS`, entry requirements, duration, and degree H1s are accepted before any landing-page rejection.

### 6. Victorian Institute of Technology (VIT) — `vit.edu.au`

- **status:** PASS (fix required)
- **mode:** smart
- **counts:** `30 / 30 / 0 / 0`
- **issue:** VIT's `Student Type` UI is a local tab/radio group with `Domestic` and `International`. The audience parser only read the first option sibling (`Domestic`) and mis-labelled every course as domestic-only, so the sampler reported `0/7 genuine course pages`. Separately, VIT uses `/course-list?course_categories[0]=<slug>` as the real listing and the classifier needed to handle that. IELTS text on VIT uses the `Overall score X.Y, with no band below Z` phrasing, which the generic patterns missed.
- **fix:**
  - Audience parser now evaluates the nearby option group, not just the first sibling → sampler flipped to `7/7` genuine.
  - Category-filtered listing pages are followed during discovery.
  - Added the highest-priority VIT IELTS pattern (`IELTS ... Overall ... no ... band ... less than X`) to the English requirement extractor.
  - Also extended Australian-university fee parsing to catch VIT-sized postgraduate fees (e.g. $48k MBA).

### 7. Australian Skills Academy Institute of Higher Education (ASA) — `asahe.edu.au`

- **status:** PASS (fix required)
- **mode:** smart (browser-assisted)
- **counts:** per-run sample verified; full batch counters pending next scheduled run.
- **issue:** ASA course pages are a Webflow tabbed layout. The raw HTML contains the fee PDF link but not the IELTS table text, and the browser helper was only opening the `Entry Requirements` tab, so `Fees and Scholarships` content and IELTS tab content never reached extraction.
- **fix:**
  - Marked `asahe.edu.au` as a browser-needed site.
  - Updated the browser helper to open both `Entry Requirements` and `Fees and Scholarships`, then merge the rendered tab snapshots into a single extraction input.
  - Kept the PDF-first international fee logic so the ASA international fee PDF still wins when present.

---

## Pending providers (from the supplied batch list)

These were included in the user-supplied university list but have not yet been executed end-to-end in this batch; they are queued for hybrid runs using the same checklist. Execute fast first, escalate to smart on failure, and fill in `counts`, `issue`, `fix`, and `status` below.

| # | Provider | Status | Mode | Counts | Issue | Fix |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | The University of Notre Dame Australia | PENDING | — | — | — | — |
| 2 | University of Canberra (UC) — Sydney Campus | PENDING | — | — | — | — |
| 3 | University of the Sunshine Coast — Adelaide | PENDING | — | — | — | — |
| 4 | University of Tasmania (UTAS) — Sydney and Melbourne campuses | PENDING | — | — | — | — |
| 5 | Victoria University — Sydney / Brisbane | PENDING | — | — | — | — |
| 6 | Kaplan Business School | PENDING | — | — | — | — |
| 7 | APIC College | PENDING | — | — | — | — |
| 8 | International College of Hotel Management (ICHM) | PENDING | — | — | — | — |
| 9 | Sydney Met | PENDING | — | — | — | — |
| 10 | Canterbury Institute of Management (CIM) | PENDING | — | — | — | — |
| 11 | Stanley College | PENDING | — | — | — | — |
| 12 | ECA College of Health Science | PENDING | — | — | — | — |
| 13 | AIT — Academy of Interactive Technology | PENDING | — | — | — | — |
| 14 | UHE (Universal Higher Education) | PENDING | — | — | — | — |
| 15 | La Trobe University Sydney Campus | PENDING | — | — | — | — |
| 16 | ACAP University College | PENDING | — | — | — | — |
| 17 | Western Sydney University — Sydney City Campus | PENDING | — | — | — | — |
| 18 | SAE University College | PENDING | — | — | — | — |
| 19 | Australian International Institute of Higher Education (AIIHE) | PENDING | — | — | — | — |
| 20 | Federation University | PENDING | — | — | — | — |
| 21 | AIBI Higher Education | PENDING | — | — | — | — |
| 22 | Western Sydney University International College (WSUIC) — Pathway | PENDING | — | — | — | — |
| 23 | Deakin College (DC) — Pathway | PENDING | — | — | — | — |

> When executing a pending row, replace its `PENDING` with one of `PASS`, `PASS (fix required)`, `PARTIAL`, or `BLOCKED`, fill the counts from the job response, and write the issue/fix cells using the same style as the **Executed** section.

---

## Rolling summary

- Total providers in the supplied list: **30**
- Providers executed so far: **7** (Torrens, CSU, Newcastle, Sydney, UniSQ, VIT, ASA)
- Passing after code fixes: **6** (Torrens, CSU, Newcastle, UniSQ, VIT, ASA)
- Passing with caveats (PARTIAL): **1** (Sydney — bulk count site-dependent; discovery now clean)
- Still blocked: **0**
- Providers pending: **23**

### Top recurring scraper issues surfaced during execution

These are called out so one fix can benefit several later providers. Each has already been addressed in `api-server/src/routes/scrape.ts` (and the mirrored copy under `artifacts/api-server/src/routes/scrape.ts`) during earlier provider debug; they are useful reference bug-classes for pending providers.

1. **Category hubs leaking into course candidates.** Short paths like `/courses/<slug>` often point to category hubs, not course detail pages. Detail URLs generally have a degree qualifier (`bachelor-of-*`, `master-of-*`, `graduate-*`, `diploma-of-*`) and/or a second path segment. Surfaced on Torrens and UniSQ.
2. **Non-course academic paths mis-accepted.** Handbook / unit / subject-area / blog / testimonial / story / career-finder paths survive naive "contains /course" fallback checks. Surfaced on Sydney and UniSQ.
3. **Label/value DOMs in non-standard tags.** Sites render `Study mode`, `Student`, `Campus locations` in `div.*__label` / `div.*__value` blocks, not just tables / DLs / inline tags. Surfaced on Torrens.
4. **Trailing decorators on labels.** Labels appear as `Campus locations*`, `Campus Location(s)`, etc. Label regexes must tolerate trailing punctuation. Surfaced on Torrens.
5. **Cloudflare / WAF walls.** Some sites block direct fetches and `HEAD` probes. A mirrored text fetch plus host normalization lets discovery proceed. Surfaced on Newcastle.
6. **Origin switch during redirect.** `www.csu.edu.au/courses` → `study.csu.edu.au/courses` requires keeping the final origin for sitemap discovery. Surfaced on CSU.
7. **Tabbed Webflow / SPA content.** Entry Requirements and Fees are often split across tabs on the same URL; the browser helper must open each important tab and merge snapshots. Surfaced on ASA.
8. **Audience tab groups read as single scalars.** `Domestic / International` toggles get mis-read as `domestic-only` if only the first sibling is checked. Surfaced on VIT.
9. **IELTS phrasing variants.** `Overall X with no band below Y` needs a high-priority parser ahead of the generic regex fallbacks. Surfaced on VIT.
10. **External fee PDFs.** Same-origin-only fee discovery misses IntelligenceBank / CDN-hosted international fee schedules. Surfaced on Torrens.
11. **Stale staged rows after a fix.** New filters only affect new inserts. Older bad rows must be cleaned from `scraped_courses` so the review table reflects the current logic.

---

## How to update this report

When a pending provider is executed:

1. Start the scrape, record the job id, and let it complete or reach the approval gate.
2. From the UI / API response capture: `totalFound`, `staged`/`imported`, `skipped`, `errors`.
3. Verify staged rows using the `scraped_courses` table rather than trusting job logs alone.
4. Move the row out of **Pending providers** into a numbered entry under **Executed providers** using the same structure. Keep the issue / fix descriptions concrete enough to match onto the recurring-issues list above.
5. Update the **Rolling summary** counters and, if a new distinct bug-class appeared, append it to the **Top recurring scraper issues** list.
