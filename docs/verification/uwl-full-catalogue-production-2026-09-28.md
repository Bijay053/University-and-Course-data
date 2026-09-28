# University of West London catalogue verification — 2026-09-28

Production host: AWS SSM-managed university portal. University ID in production: 81. Release revision observed on worker: `b491ce8e98af713e0563a5cca89cbf84398b4a6b`.

Both new jobs were started with `fullCatalogueReviewOnly=true` and `forceDiscovery=true`. No stopped job was revived and no other active job was interrupted.

## First run: `job_59661473ed92`

- Static Scrape.do failed with `502 ROTATION_FAILED` on the first listing page; the worker switched to rendered mode, and rendered pages returned course links.
- Rendered pages 2 and 7 (zero-based page parameter) subsequently returned provider HTTP 502. The other 10 pages added 267 links; after filtering, discovery reported 282 extractable URLs, below UWL's configured 300-course minimum.
- An external stop ended this review-only run during extraction at 74/282; the terminal job state was `stopped`, with **zero staged rows** and no publication.

## Operator-authorized fresh retry: `job_27f12d51271e`

- Static-to-rendered failover activated. All **12/12** rendered listing pages returned course links, with **0 unavailable** listing pages. They added 318 links, yielding 340 raw candidates and **333 extractable course URLs**, above the 300-course minimum.
- Terminal state: `completed` at 2026-09-28 12:05:01 UTC. Found/processed: **333/333**; staged: **265**; skipped: **68**; fetch failures: **0**; errors: **0**. The conservation check holds: 265 + 68 = 333.
- Skip reasons: `no_international_fee` 30, `part_time_only` 21, `category_landing_page_missing_degree_qua` 15, `online_only` 2.
- All 265 rows are **pending** (`review`: 263; `data_quality_failure`: 2). The review-only policy records `automatic_publish=false`; the live UWL courses table contained 0 rows, with 0 modified since this retry started. There were 280 pending UWL rows total, including 15 older pending rows.
- The policy's `full_catalogue_verified` field remains `false` (the review-only policy initializes this field as false and does not automatically change it when the scrape completes). This record verifies the listing-page coverage and staging counts; it is **not** an approval of individual courses or permission to publish.

The first run demonstrates that a provider 502 on nonconsecutive rendered pages can omit links without the current two-consecutive-failures abort. The successful retry establishes full listing-page availability and coverage at the time of this check, but future runs should fail closed if a configured listing page is unavailable rather than rely only on a minimum-count floor.