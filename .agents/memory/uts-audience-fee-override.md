---
name: UTS audience-state fee override
description: Why complete-looking UTS static fee data must be replaced by the browser-selected International state.
---

Treat UTS static course-page fees as non-authoritative because the server response defaults to the Domestic audience while still containing plausible first-year and full-course amounts. A required International browser interaction must run even when the static payload passes completeness gates, and the rendered fee amount, term, year, and currency must replace their static counterparts as one unit.

**Why:** A complete static payload prevented the browser interaction from running. When the browser was later forced, the extended merge replaced only the numeric amount and silently retained the static `Session` term, so the full-course-to-annual conversion could not run.

**How to apply:** For audience-dependent pages, required browser state outranks field completeness. Use full rendered extraction with override semantics, preserve fee metadata with the amount, and only annualize an explicitly identified full-course total by the full-time duration.