---
name: Canterbury embedded course-year facts
description: Authority and year-selection rules for Canterbury Christ Church course fees and English requirements.
---

Canterbury Christ Church course pages embed the authoritative selected entry-year record in Contensis Redux state. Read fee tables and the international English requirement from that selected record rather than from flattened script text or an institutional fee default.

**Why:** Generic extraction cannot see the HTML fragments inside the script payload. A stale blanket fee default made integrated foundation-year degrees look cheaper, while pathway classification suppressed their valid standard IELTS requirement. Future entry-year records may explicitly have no finalised fee.

**How to apply:** Match the URL's `year` query to the embedded entry year. Treat a Bachelor's degree “with Foundation Year” as a full degree, not a standalone pathway. Do not substitute current fees into an unpublished future-year record; exclude or reject that future row and retain the verified current-year offering.