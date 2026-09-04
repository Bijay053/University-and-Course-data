---
name: Protected-site URL onboarding
description: Reliability and repair rules for university metadata discovery when homepages block direct HTTP.
---

Treat block/challenge HTML as a fetch failure during add-by-URL onboarding, then escalate to a rendered fetch. Re-adding an existing URL must upgrade hostname-derived names and incomplete location data rather than returning the stale record unchanged.

**Why:** SEGi’s homepage returns a Cloudflare 403 to direct HTTP, so onboarding stored “Segi” and “Unknown.” Rendered HTML exposes authoritative branding and JSON-LD, but the rendering provider can also transiently fail; a verified institution-specific fallback may still be needed for deterministic repair.

**How to apply:** Never parse challenge titles as metadata. Normalize JSON-LD country codes and compound localities, deduplicate repeated Place/Organization addresses, refresh only unverified discovered locations, and preserve all operator-verified location edits.

When deterministic metadata yields a short brand and no locations, use OpenAI only as a page-grounded normalizer: pass compact official-page evidence, require an exact evidence quote, and reject hostname/country mismatches. It may expand a weak brand into a longer official institution name only when the original words remain in order, and AI locations must never overwrite operator-verified rows.

**Why:** Notre Dame’s homepage advertises `og:site_name` as only “Notre Dame” and has no JSON-LD addresses, while its meta description and footer explicitly provide the full legal name and Fremantle, Broome, and Sydney campuses.

**How to apply:** Keep the AI call bounded and fail-safe. If credentials, evidence, confidence, hostname, or country validation fails, retain deterministic metadata rather than accepting an unsupported identity.