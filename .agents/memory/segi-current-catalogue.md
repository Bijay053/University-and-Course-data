---
name: SEGi dual current catalogue transport
description: Authoritative sources and transport boundaries for SEGi University and Colleges scraping.
---

Treat `university.segi.edu.my` and root-level programme pages on `www.segi.edu.my` as distinct current catalogues under SEGi University & Colleges. Keep the university catalogue primary and supplement it with qualification-bearing WordPress pages indexed by the `Programme ID` marker.

**Why:** The university host has the university catalogue and an incomplete TLS chain, while the Cloudflare-protected main host publishes a separate, current, campus-specific college catalogue. Broad WordPress search also returns marketing, policy, and faculty pages, and generic render retries can multiply paid calls.

Scrape.do can return persistent `ROTATION_FAILED` responses for the Colleges host across standard, super, geo-pinned, and asynchronous pools. Those transport failures do not establish the cause of an extraction stall.

**Measured CPU stall:** A live worker stack showed Stage-0 generated CSS stuck
in SoupSieve's repeated ancestor matching after HTML had arrived. The English
rule combined one sibling relation with six descendant relations; thresholds
must count the combined chain, not just each relation type separately.

**Why:** Async deadlines cannot preempt synchronous CSS matching, and a
stopped job can leave its worker consuming CPU. Provider-only probes omitted
the database-generated rules and therefore did not reproduce the stall.

**How to apply:** Verify the exact saved rules against real HTML, bound generated
selector complexity before matching, and retain normal extraction fallbacks.
Do not call a network-only probe a proof that full job extraction is fixed.

**Enrichment ordering:** Confirmed course-owned online-only delivery should
exit before remote AI/OCR work, while mixed on-campus options remain eligible.
**Why:** Removing a selector stall still left a serial catalogue spending
remote enrichment time on courses the final eligibility gate would reject.
**How to apply:** Use authoritative evidence, not generic page-wide "online"
text; preserve the normal skip flags/counters. Do not assume a selector fix
alone proves that increasing parallel extraction is safe.

**How to apply:** Keep the university host on exact-host, HTTPS-only direct TLS handling with redirect revalidation. Discover the main-host supplement through bounded rendered WordPress search, require exact host/root path plus programme and qualification evidence, and bound every rendered attempt below the whole-course deadline so provider failures cannot stale the worker heartbeat. Retry only within that deadline and require a successful single-page provider probe before launching a full production proof. Read campus only from course-owned metadata/hero content; never use a global SEGi campus default. Treat explicit “Online Mode” titles as authoritative Online delivery. Leave unpublished tuition blank rather than inferring ancillary charges.

**AI text preparation:** Do not classify every class containing `widget` as
boilerplate. SEGi Colleges uses Elementor, where legitimate programme content
is wrapped in `elementor-widget-*`; broad stripping can turn a 600 KB rendered
page into empty AI input.

**Why:** Empty input made enrichment guess unsupported duration and English
values that were later rejected or staged with weak provenance.

**How to apply:** Restrict widget removal to explicit sidebar/footer widget
regions, choose the richest content container rather than the first matching
WordPress mount, and fall back to the stripped full document when named
containers are only empty shells.