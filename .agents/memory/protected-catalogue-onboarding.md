---
name: Protected catalogue onboarding
description: Ordering, classification, and pagination rules for automatically configuring protected course catalogues.
---

New-university onboarding must commit its visible `probing` state before publishing background configuration work. Scrape entry points and automatic first-scrape scheduling must use the same per-university lock and the same freshness rule for queued jobs.

**Why:** A fast worker can finish before the request thread resumes; committing after publication can overwrite `configured` with stale `probing`. Different start paths or stale queued-job policies create duplicate work or permanently suppress the first scrape.

**How to apply:** Persist and commit the state transition first, publish second, and condition any publish-failure rollback on the row still being in the intermediate state. Recheck onboarding state and active jobs while holding one shared advisory lock.

Observed XHR traffic is not course-search evidence by itself. Engagement, ambassador, chat, analytics, and marketing endpoints must be excluded unless the response contains course-shaped records.

**Why:** Third-party engagement widgets often expose GraphQL or search-like calls that look structurally plausible but return people or marketing content, producing polluted discovery.

**How to apply:** Keep raw traffic for diagnostics, denylist known engagement families, and require course-shaped response evidence before selecting an API provider.

HTTP 200 is not proof of a usable protected-site response. Empty packed-script documents and vendor challenge shells must be treated as retryable failures.

**Why:** Residential/rendering providers can return a complete-looking 200 document containing only anti-bot JavaScript; accepting it silently loses a catalogue page.

**How to apply:** Detect narrow vendor signatures or script-only packed documents, retain visible-content negative fixtures, and keep challenge rejection inside the bounded retry ladder.

Semantic pagination queries must never be removed during fetch fallback, and failed prefetched seeds must retry the exact original URL.

**Why:** Dropping `page`, `offset`, `start`, or equivalent parameters turns every failed page into page zero, creating a plausible but incomplete catalogue that can pass ordinary deduplication.

**How to apply:** Protect pagination keys from bare-query fallback, let cached prefetch failures fall through to a real exact-URL fetch, and test both the retry URL and absence of an unpaginated request.