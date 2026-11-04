---
name: LSBU render and delivery evidence
description: Why LSBU extraction needs a constrained browser fallback and course-owned online labels.
---

LSBU course pages are accessible through a rendered proxy, while direct requests hit bot protection. On these large rendered pages, the separate per-course browser pass added no missing fields in the observed scrape and consumed the per-course deadline. Keep that pass disabled unless a fresh source comparison proves it supplies an essential field.

**Why:** A large LSBU scrape ended with many per-course timeouts, and the browser passes visible in its log filled nothing. Page-wide study-mode rules also classified campus courses as Online, causing erroneous online-only skips. Broadly disabling Online detection would create the opposite error.

**How to apply:** Limit render concurrency, avoid redundant browser work, and preserve explicit course-owned delivery labels while ignoring utility/navigation text. Treat archived URL discovery counts as candidates, not proof of a current catalogue. Verify bounded live-source probes and then a monitored review run before claiming full catalogue recovery. Full-catalogue review intentionally removes sampling caps; do not assume an unsupported request field limits it.

Disabling browser rescue alone does not bound the proxy retry ladder. A failed rendered request can still consume the whole course deadline before archive fallback is reached.

**Why:** A later LSBU log still took 94 minutes with browser passes disabled, and then started a separate large retry. One live current-page fetch succeeded within 20 seconds while a legacy archived URL remained unavailable.

**How to apply:** Budget the first live attempt independently and reach archive fallback before generic static/render retries exhaust the course deadline. Verify both current and unavailable URLs; a successful fetch is not proof of extraction quality or full catalogue recovery.

An early archive fallback also needs an end-to-end budget including its
semaphore wait, not only an HTTP timeout.

**Why:** Cached archive replay serializes through a single slot and may perform
three 25-second attempts plus retry sleeps. A failing replay can therefore
consume other courses' deadlines while they wait without making a request.

**How to apply:** Bound the queue and replay together, cancel timed-out work
cleanly, and retain the bounded final live attempt. Keep unresolved URLs as
explicit recoverable failures; do not present this transport bound as proof
that the full archived catalogue is current or that AI extraction is fast.

Use LSBU's dedicated current course-search collection as catalogue authority,
not a historic CDX inventory or the general site-search meta collection.

**Why:** A cached archive inventory contained retired slugs and duplicates.
More than half failed, while a retired music-course URL returned a generic
“Search Module” shell. The dedicated current collection returned working
replacement addresses, including Adult Nursing. The general meta collection
also includes separate web records and is not an equivalent course inventory.

**How to apply:** Re-discover through the verified course collection and check
live course-owned titles before a full retry. Preserve bounded archive transport
for already identified courses; discovery authority and page transport are
different decisions. A successful live catalogue must not be supplemented with
historic CDX addresses. Check the effective archive mode after configuration
writes: older loaders can ignore null admin overrides. Never stage the search
shell as a course.

When configuring the current production collector, specify the API's actual
page-size query parameter as well as its pagination size.

**Why:** The older deployed collector advanced offsets using the configured
size but did not send that size to the API. The API defaulted to ten results,
which appeared to be a last page.

**How to apply:** Keep both values consistent until the collector update is
published, and verify the full provider result rather than accepting any
positive first-page count.