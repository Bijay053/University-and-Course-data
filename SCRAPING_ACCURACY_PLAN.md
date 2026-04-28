# Scraping Accuracy Plan — Implementation Guide

This document complements the ChatGPT recommendation
(`Recommendation: Better scraping architecture with AI page discovery`)
and explains **how to actually make the system work without errors and
with high accuracy**, mapped to the current codebase.

---

## 0. Guiding Principles (read first)

| Principle | Rule |
|---|---|
| **Code is law, AI is a hint** | Every AI answer must be re-validated by code before it changes the DB. |
| **Source evidence or reject** | No field is saved without a `source_url` and `source_text` that proves it. |
| **Fail loud, never guess** | If a value can't be found from a real page, set it to `NULL` and route to review — never invent a value. |
| **Idempotent and rerunnable** | Re-scraping the same university must produce the same result, not duplicates. |
| **Provider config beats global heuristics** | Each university gets its own pinned URLs and rules. Generic heuristics are the fallback. |
| **One change at a time** | Land each phase, scrape one university, verify, then move to the next. |

---

## 1. Where each recommendation lives in the codebase today

| Recommendation | Current file(s) | Status |
|---|---|---|
| Crawl from homepage | `services/scraper/discovery.py` (BFS) | Exists, needs link-keyword scoring |
| URL/title scoring of candidate listing pages | `discovery.py`, `page_type.py` | Partial — scoring exists but is not exposed as a ranked candidate list |
| AI ranking of top-N candidates | `extractors/ai_fallback.py`, `extractors/gemini_primary.py` | Exists for fields, NOT for page-discovery |
| Verify listing page has ≥10 real course links | `guards.py`, `discovery.py` | Missing — must be added |
| Reject category/info/apply pages | `category.py`, `guards.py` | Partial — needs explicit blocklist |
| Hidden API capture during Playwright | `browser_pool.py`, `browser_discover_generic.py` | Missing — needs `page.on("response")` hook |
| Per-provider config | DB column `university.scrape_config` (JSON) | Missing column — needs migration |
| Source evidence per field | `provenance.py` | Exists — must be enforced everywhere |
| Auto-publish vs review rules | `approve_course.py`, `completeness.py` | Exists — needs confidence threshold |

---

## 2. Phased Rollout (do in this exact order)

### Phase A — Safety net first (1 day, no behaviour change)

Goal: stop publishing bad data **before** changing how we discover pages.

1. **Enforce provenance on every saved field.**
   - In `services/scraper/stagers/`, every extractor result must carry
     `(value, source_url, source_text, confidence)`. Reject the field at
     stage time if any of those are missing.
   - Acceptance: a unit test that feeds an extractor result with no
     `source_text` and asserts the field is dropped, not saved.

2. **Add a confidence floor on auto-publish.**
   - In `approve_course.py`, do not auto-approve a course whose overall
     confidence < 85, even if `completeness.py` says all fields present.
   - Acceptance: a course with all fields but confidence 70 stays in
     `review` queue.

3. **Hard reject pages by URL/title pattern.**
   - In `guards.py`, add a single function `is_blocked_page(url, title)`
     returning `True` for `apply, fees, scholarships, key-dates, blog,
     news, events, school, faculty, testimonials, contact`.
   - Call it from `discovery.py` BEFORE adding a URL to the queue and
     from `single_course.py` BEFORE staging a course.

After Phase A: re-scrape one university, confirm queue items dropped
into review look reasonable, and **nothing publishes that previously
needed manual fixing**.

### Phase B — Better discovery (2 days)

4. **Add a link-keyword scorer.**
   - New file `services/scraper/discovery_score.py`.
   - Pure function `score_listing_candidate(url, title, anchor_text, html_snippet) -> (score:int, reasons:list[str])`.
   - +30 if URL contains `/courses`, `/degrees`, `/programs`, `/find-a-course`.
   - +20 if title contains `courses|degrees|programs`.
   - +15 if HTML contains course-search filters (look for `data-filter`, `<select name=*level*>`, etc.).
   - +10 if page links to ≥10 internal URLs that look like course detail pages.
   - −50 for any blocklist match from Phase A step 3.
   - 100% pure function with unit tests; no IO.

5. **Expose top-5 candidates and verify before saving.**
   - In `discovery.py`, after BFS, sort candidates by score descending,
     take top 5.
   - For each candidate, fetch the page and run a **code verifier**:
     - Has ≥10 internal links whose path/text looks like a course name.
     - Page is not in the URL blocklist.
     - Page is on the same registrable domain as the homepage.
   - Pick the highest-scoring **verified** candidate. If none verify,
     mark discovery as failed for that university and surface in admin.
   - **Never** save an unverified candidate even if AI says it is best.

6. **Optional AI tiebreaker (only when scores are close).**
   - If the top 2 verified candidates are within 10 points of each other,
     ask Gemini to rank them with the JSON schema in the recommendation.
   - Validate AI's chosen URL is one of the inputs (do not accept new
     URLs from AI). If AI picks an URL not in the candidate set, ignore.

### Phase C — Per-provider config (1 day)

7. **Add `scrape_config` JSONB column on `universities`.**
   ```json
   {
     "course_listing_url": "https://www.unisq.edu.au/study/degrees-and-courses?studentType=international",
     "course_detail_url_pattern": "/study/degrees-and-courses/[a-z0-9-]+",
     "scraping_method": "playwright",
     "url_query_params": {"studentType": "international"},
     "known_reject_patterns": ["/study/online", "/about/"],
     "known_accept_patterns": ["/study/degrees-and-courses/"],
     "last_verified_at": "2026-04-28T00:00:00Z",
     "confidence_score": 95
   }
   ```
   - Migration: add nullable column; existing rows get `NULL` → fall back to current generic heuristics.
   - Once a discovery run succeeds for a university, persist the verified
     listing URL into this column so subsequent runs skip discovery.

8. **Centralise URL-rewriting (international query params).**
   - Move `?studentType=international` (UniSQ), `?students=international`
     (UOW), `?international=true` (UNE) out of `single_course.py` into
     `scrape_config.url_query_params` per provider.
   - One helper: `apply_provider_url_params(url, university)`.

### Phase D — Hidden API capture (2 days, advanced)

9. **Capture XHR/JSON during Playwright runs.**
   - In `browser_pool.py` (or wherever the page is created), register:
     ```python
     page.on("response", lambda r: maybe_record_api_response(r, journal))
     ```
   - Heuristic: record only `application/json` responses ≥1KB whose URL
     is on the same registrable domain.
   - After the run, inspect captured payloads:
     - If a JSON contains an array of objects each with at least
       `name|title` + `url|slug`, mark it a candidate course-listing API.
   - Save the API URL into `scrape_config.scraping_method = "api"`.

10. **Switch to API path when one is known.**
    - On the next run, if `scrape_config.scraping_method == "api"`,
      fetch JSON directly with `httpx` (no Playwright), iterate items,
      build detail URLs, then fetch each detail URL with the existing
      single-course pipeline.

### Phase E — Per-field source evidence everywhere (1 day)

11. **Tighten extractor return type.**
    - All extractors in `services/scraper/extractors/` already return
      `ExtractionResult`. Make `source_text` and `source_url`
      **required** (not optional). Update each extractor that doesn't
      currently set them.
    - Provenance is already a model — make sure it's written every time
      a field lands in `course_field_versions` (or wherever the source
      of truth is).

12. **Show evidence in the review UI.**
    - The Review Scraped Courses page should show, for each field, a
      small "source" link (the `source_url`) and a snippet (the
      `source_text`). This makes human review fast and turns errors
      into 1-click fixes.

### Phase F — Tests that make this stick (continuous)

13. **Frozen-HTML tests.**
    - For each university, save 3 known-good detail pages as fixtures in
      `backend-py/tests/fixtures/<provider>/` and assert the extractor
      pipeline produces the exact expected fields. These pages should
      include the previously buggy ones (UniSQ MPH, Flinders Sport,
      etc.) so regressions are caught.

14. **Discovery smoke test.**
    - For each university with `scrape_config` set, run discovery and
      assert the discovered listing URL equals `scrape_config.course_listing_url`.

15. **Block-listed page test.**
    - Feed a list of known-bad URLs (apply page, fee page, news article)
      and assert `is_blocked_page` returns True for all.

---

## 3. Hard rules to remove the most common errors we have seen

| Error class | Root cause we hit | Hard rule going forward |
|---|---|---|
| Wrong fee (domestic shown) | Missed `?studentType=international` for UniSQ | All international URL params go through `scrape_config.url_query_params`. Code MUST refuse to save a fee whose source page URL does not contain the international param when the provider has one. |
| PTE = 14 from "September" | Substring match on `pte` inside month names | All English-test patterns must use `\b` word boundaries (already fixed). Add a unit test that asserts "Monday 14 September" yields `pte=None`. |
| Garbage location ("Ipswich External Start Feb…") | Concatenated table cell text | Location output must match a known-campus name **OR** be `NULL`. If the candidate string contains delivery-mode words ("external", "online") we drop them; if what's left isn't a known campus, we set `NULL` and send to review. |
| Course discovered that's actually a category page | No verifier on listing pages | Phase B step 5: ≥10 real course-detail links required before a page is treated as a listing. |
| Auto-published with wrong campus | Auto-publish ignored confidence | Phase A step 2: auto-publish requires confidence ≥85. |
| Duplicate courses on re-scrape | Slug not stable | Course identity = `(university_id, normalized_url_path)`, not `(name)`. This is already the case — keep it that way and add a uniqueness test. |

---

## 4. Where AI is allowed (and where it is not)

| Allowed | Not allowed |
|---|---|
| Rank a fixed list of candidate listing URLs | Generate listing URLs |
| Classify an unclear page into one of fixed labels | Decide a course should be published |
| Extract one missing field from a known-good detail page text | Extract all fields blindly |
| Validate "is this an international on-campus course?" given the page text | Decide what fields to store |

Every AI call must:
1. Have a strict JSON schema response.
2. Fail closed: if AI doesn't return valid JSON, the call is treated as "no answer".
3. Be logged with the prompt, response, and the URL it was asked about.

---

## 5. Definition of done for each university

A university is "scraping-ready" when:

- [ ] Its `scrape_config` row is populated with a verified listing URL.
- [ ] A scrape run produces ≥ N courses (N = expected per provider; e.g. UniSQ ≥ 60).
- [ ] All produced courses have non-NULL `course_name`, `degree_level`, `study_mode`.
- [ ] No auto-published course has confidence < 85.
- [ ] At least 3 fixture pages for that provider pass the frozen-HTML test.
- [ ] Re-running scrape on the same provider produces zero new "review" items
      (i.e. the run is stable).

---

## 6. Suggested order of work

1. Phase A (safety net) — tonight.
2. Phase E step 11 (provenance enforcement) — tonight, alongside A.
3. Re-scrape UniSQ, UOW, UNE, Flinders → confirm no regressions.
4. Phase B (discovery scoring + verifier) — next.
5. Phase C (per-provider config + migration).
6. Phase F (tests) — incrementally as each phase lands.
7. Phase D (hidden API) — last, only after the above is stable.

Do not start a new phase until the previous phase's "definition of done"
is met for at least 2 universities.
