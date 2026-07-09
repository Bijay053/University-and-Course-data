---
name: Multi-campus slug collision
description: A satellite/branch campus university record can get accidentally overwritten with another university's YAML config due to slug/domain confusion.
---

## Incident

"University of Hull (London)" (uni_id=2227, `scrape_url=https://london.hull.ac.uk/`) is a
**separate small satellite campus** from the main "University of Hull" (`www.hull.ac.uk`,
~471 courses, not yet in this DB). The slug deriver produces different slugs for each
(`london` vs `hull`), but `london_2227.yaml` had been populated with the main campus's
`www.hull.ac.uk` seed_urls/config by mistake — likely from working on both universities in
the same session and writing to the wrong filename.

Result: 100% of discovered course links pointed to the wrong domain, and the domain guard
correctly blocked all of them (`[DOMAIN GUARD] 189/189 links... are from a foreign domain`).

## Rule

Before trusting or reusing an existing `{slug}_{id}.yaml`, verify its `seed_urls`/`sitemap_url`
domain actually matches the DB row's `scrape_url` apex domain for that `id`. A domain-guard
100%-block log is the tell — it means the YAML's seed content belongs to a different host
than the university record it's attached to.

**Why:** satellite/branch campuses ("University of X (City)", "X London", "X International")
are frequently separate DB rows with distinct domains and tiny catalogues, easily confused
with the parent institution's much larger main-campus config during multi-university sessions.

**How to apply:** when picking up a new university-scrape bug report, always re-derive the
slug from the DB's actual `scrape_url` and diff it against any existing YAML content's
seed URLs before assuming the file is correct — don't just check the filename.

## Follow-up (2026-07-09): file reverted after the fix was committed

After committing the London-campus fix, the working-tree copy of `london_2227.yaml` was
later found silently reverted back to the wrong main-campus (`www.hull.ac.uk`) content and
staged again — with no corresponding user edit. Always re-`read` a just-fixed per-uni YAML
file (don't trust memory/summary of its content) before debugging a "still broken" report
against it; compare against `git show HEAD:<path>` if the content looks unexpectedly wrong.

## Debugging gotcha: `extract_course()` return shape

`single_course.extract_course()` returns `{"url":..., "payload": {...fields...},
"evidence": [...]}` — NOT the flat fields dict directly. Calling
`result.get("course_name")` on the top-level return always gives `None` even when
extraction succeeded; use `result["payload"].get("course_name")`. Wasted significant
debugging time chasing a phantom "course_name extracts as None" bug that was actually
this test-harness mistake.
