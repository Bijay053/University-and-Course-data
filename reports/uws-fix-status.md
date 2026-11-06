# UWS extractor and old-assistant correction checkpoint

Production read-only inventory: 131 pending UWS rows. Row 56120 is absent; only undergraduate Accounting row 56381 remains. Accounting and Finance is a separate postgraduate course. No rows deleted or changed.

Live source extraction preview: 94 rows have proposed name, duration or intake scalar corrections. Campus date and pathway evidence retained for the cohort. Multi-award ambiguity is flagged rather than silently collapsing awards. Preview: uws-extraction-preview.json and uws-corrections-preview.html.

Workspace fixes: UWS banner award/title composition; full-time duration parsing including commas and standard versus sandwich pathways; campus start-date evidence and entry restrictions; old assistant no longer reimports an existing course from another review merely on fix/correct requests.

Validation: 40 focused extractor/router/agent-loop tests passed. Old-assistant regression had 306 passing tests and two assertions expecting the old reimport behavior; those assertions were updated to enforce the requested no-duplicate behavior and both targeted reruns passed. A real-model isolated-fixture single-row name edit passed and the stored name was verified. No production model writes occurred.

After owner approval, the production transaction corrected 94 rows and preserved source evidence with audit records across all 131 pending rows. A separate read verified every changed value. Version-fenced undo receipts are in uws-undo.json. Two maintenance tests verified repair/undo and rejection of stale plans.

The exact narrow release tree passed 11 tests and was published to GitHub on uws-extractor-release. Its commit is recorded in uws-release-target.json. The guarded AWS release refused it before checkout because it requires the target to equal origin/main. Main contains 13 previously queued files (383 additions, 8 deletions), including intake/field editing, eligibility, source caching and Excelsia extraction changes. These were deliberately not included in the narrow tested release.

The owner subsequently approved the combined release. The exact combined tree passed all 347 regression tests (one existing Pydantic deprecation warning) and GitHub main was fast-forwarded to the tested release recorded in uws-release-target.json.

The AWS guard then passed revision, runtime and schema checks but refused restart smoke because one scrape remained running. It stopped before checkout and before any smoke-sample write. Production code therefore remains at its predecessor; API, Celery and Nginx remain active. The existing failed-release evidence was retained.

A bounded idle watcher is running: it will wait up to 600 seconds, then run the guarded release once if no scrape jobs remain active. It will not cancel jobs or bypass the guard. A separate observer records the eventual result in uws-release-final-observation.json. No new-agent activation occurred; signed-in UI/new-agent end-to-end verification remains outstanding.
