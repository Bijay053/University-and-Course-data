---
name: Essex TLS and subject catalogue
description: Secure chain completion and authoritative catalogue sources for Essex.
---

A browser succeeding while Python reports an unknown certificate issuer does
not justify disabling TLS verification. Essex's Let's Encrypt YR2 chain can be
completed using the official YR2 issuer and Root YR cross-signed by the already
trusted ISRG Root X1.

**Why:** A verified request succeeded after adding this chain. Adding only YR2
was insufficient. The compatibility chain must not become a new independent
trust anchor or reintroduce removed roots.

**How to apply:** Obtain issuer files over verified HTTPS from the CA; require a
complete path to existing trusted roots, disable partial-chain trust, retain
hostname checks and operator trust-bundle settings. Review the compatibility
certificates when the issuer rotates.

Essex's content sitemap is not its degree catalogue. Its subject directory
contains course links wrapped in official Funnelback redirects, often with a
startdate query parameter.

**Why:** Sitemap parsing returned no degree routes despite a large valid XML
response; browser navigation from the homepage wandered into study-abroad and
generic masters/research pages instead.

**How to apply:** Traverse verified subject listings, unwrap course redirects,
preserve dated queries and require the course-code/route/slug detail shape.
Navigation pages and course URLs must not share the same extraction eligibility.
