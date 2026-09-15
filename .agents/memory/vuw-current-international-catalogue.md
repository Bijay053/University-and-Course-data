---
name: VUW current international catalogue
description: How to avoid stale fees and mismatched intake years in Victoria University of Wellington’s JSON provider.
---

Fetch VUW’s programme endpoints with the international query parameter. Treat the bare endpoint response as potentially stale even when it returns HTTP 200 and structurally complete records.

**Why:** The bare endpoint served prior-year fees while the same endpoint with an international/cache-varying query returned the current values shown in the live international course page. Numeric intake months also bypassed persistence expectations and mixed placeholder or prior-year dates into review.

**How to apply:** Use the international endpoint variant, emit canonical month names, ignore 1970 placeholders, and prefer intake dates matching the published fee year. Preserve the API’s international location as course-owned evidence.