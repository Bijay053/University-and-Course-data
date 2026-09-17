---
name: Algolia provider trust boundaries
description: Safety rules for typed catalogue records and provider metadata across extraction retries.
---

Provider-backed links that bypass shared discovery filters must enforce their
record type and URL ownership inside the provider before links are emitted.

**Why:** A broad page index can mix genuine programme records with subject and
marketing pages. Treating every provider hit as trusted defeats later YAML
allow/block patterns when provider links intentionally bypass those filters.

**How to apply:** Configure exact provider-level URL patterns and required
record-field values. Keep these filters default-empty so existing providers
retain their behavior.

Authoritative provider metadata must survive every retryable extraction result
and be restored onto recovery links.

**Why:** Dropping audience-scoped metadata during recovery can make domestic or
online-only pages look eligible when extraction retries against a domestic-
default HTML page.

**How to apply:** Carry the original provider payload through timeout and error
sentinels, and assert retry-queue preservation in tests whenever provider
metadata controls staging eligibility.