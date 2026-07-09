---
name: UC Leeds degree-qualifier false rejection
description: Per-course H1 title lacking a degree-level word can wrongly trigger the category-landing-page guard even on fully-extracted courses.
---

Some universities render the course-detail H1 as a bare subject name ("Acting", "Biosciences") with the degree suffix ("BA (Hons)") appearing only in the page `<title>`/breadcrumb/discovery-link text, not the H1 itself. `guards.should_stage_course()`'s `_name_has_degree_qualifier()` check then rejects these as `category_landing_page_missing_degree_qualifier` even when the course fully extracted (100/80 field completeness) — the log line only shows `PASS 100/100` earlier, so the rejection reason at the end of the run is the tell, not the per-course pass score.

**Why:** The check exists to filter real category/hub pages, but it assumes the qualifier always lives in the H1. Some CMS templates split it out.

**How to apply:** If a per-uni run shows most/all `PASS` courses ending up in `Skipped` with reason `category_landing_page_missing_degree_qualifier`, set `extraction.staging.skip_degree_qualifier_check: true` in that university's YAML (same flag already used for ARU/Writtle) — safe as long as discovery-time URL/hub filters already exclude the real category pages. Also: restarting the Celery worker while a verification scrape is running kills the job with `error_message: "Worker restarted — slot freed on startup"`; don't restart workflows mid-scrape when trying to verify a fix.
