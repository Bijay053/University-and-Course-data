---
name: Lancaster cookie-modal H1 trap
description: Lancaster's cookie-consent modal injects an h1 before the course title; soup.find("h1") returns the wrong element and causes 0-staged runs.
---

# Lancaster cookie-modal H1 trap

## The rule
When `soup.find("h1")` is used to extract the course name, it returns the first H1 in document order. Lancaster's cookie-consent modal injects `<div id="biccy-prompt"><h1>Our use of cookies</h1></div>` early in the DOM (before `<main>`), so the extractor always returns "Our use of cookies" instead of the real course title.

**Why:** All 538 courses rejected as `category_landing_page_missing_degree_qualifier` — "Our use of cookies" has no degree qualifier word (BSc/MSc/etc.), so `_stage_gate` refuses to stage any course.

## The fix
`CourseNameConfig.h1_css_selector` (Optional[str]) — per-uni YAML field that overrides `soup.find("h1")` with `soup.select_one(selector)`, falling back to `soup.find("h1")` if the selector matches nothing.

Lancaster YAML:
```yaml
extraction:
  course_name:
    h1_css_selector: "div.course-title h1"
```

The real course title lives in `<div class="course-title"><h1>Course Name BSc Hons</h1></div>` inside `<main>`. The CSS selector skips the cookie modal.

## How to apply
Any university whose CMS injects a cookie-consent or tracking-consent H1 before the main content should set `extraction.course_name.h1_css_selector` in its per-uni YAML. Check with `soup.find_all("h1")` to confirm multiple H1s exist and which one holds the real title.

## Don't confuse with
- YAML `selectors.course_name.xpath` — that's the Phase-2 AI-rule path (`ai_extractor_run.py`), NOT the built-in `course_name.py` extractor. These are independent code paths.
- `scrape_do_static` / `scrape_do_geo` — those route the HTTP fetch through Scrape.do to bypass geo-detection. They do NOT fix the H1 ordering issue; even the correct page has the cookie banner H1 first in the DOM.
