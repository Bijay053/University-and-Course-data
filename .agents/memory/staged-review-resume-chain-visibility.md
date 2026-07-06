---
name: Staged-course review visibility across resumed scrape runs
description: When a scrape resumes across multiple runtime_job_ids, per-job_id staged-course queries hide legitimately-pending rows from earlier jobs in the chain.
---

The task229 resume checkpoint intentionally lets a new scrape run pick up
where an interrupted (failed/stopped/worker-restarted) run left off, skipping
URLs already staged under the OLD job_id rather than re-scraping them. Those
older rows stay `status='pending'` and are never re-tagged with the new
job_id.

**Why this matters:** any UI or API path that lists "staged courses for a
scrape run" by filtering `scrape_job_id == X` will only show the slice
staged by that specific run, even though the true reviewable set is
"all pending courses for the university" spread across the resume chain.
This looks like data loss to an operator (e.g. "122 staged, 59 skipped, rest
missing out of 409" when the true total staged was 350 across 3 job_ids).

**How to apply:** when building a review/list surface for scraped_courses
tied to a job, scope the query by the job's `university_id` + `status`
(e.g. `pending`), not by exact `scrape_job_id` equality — unless the surface
is explicitly a historical/per-run audit view (e.g. the runs-history list's
per-job `stagedCount`), where per-job_id counts are correct and intentional.

Diagnostic pattern: compare `scrape_runtime_jobs.total_found` /
`.imported` / `.skipped` for the job (these should sum correctly) against
`SELECT COUNT(*) FROM scraped_courses WHERE scrape_job_id = '<job>'` — if the
latter is much smaller, check for other job_ids for the same university with
pending rows (`GROUP BY scrape_job_id, status`) before assuming courses were
lost.
