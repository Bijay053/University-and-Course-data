---
name: Plymouth sitemap and delivery evidence
description: External Plymouth catalogue defects that affect discovery and online-only classification.
---

Plymouth's official courses sitemap has published many postgraduate awards under `/courses/undergraduate/`, even though those exact pages return 404 and the matching `/courses/postgraduate/` pages are live. Do not assume the official sitemap is a live-URL authority; verify both routes before changing an individual course identity.

**Why:** A large scrape spent most of its failures fetching these dead postgraduate URLs, while a healthy undergraduate course at its published route remained valid.

**How to apply:** On future Plymouth catalogue changes, check representative source and replacement URLs with the site's current response, and preserve the original route if it still serves a course or the replacement cannot be verified.

Plymouth course pages can also include an Academic Partnerships paragraph about the option to “engage in distance learning” at partner institutions. This describes the broader partnership programme, not necessarily the delivery mode of the course whose page contains it.

**Why:** Page-wide phrase matching treated this paragraph as strong Online evidence and triggered the fleet-wide online-only exclusion for campus courses.

**How to apply:** Use course-owned labels or programme-specific delivery text for Online decisions; never weaken the global online-only eligibility rule to compensate for this boilerplate.