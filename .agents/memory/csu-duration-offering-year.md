---
name: CSU duration and offering-year authority
description: How to resolve conflicting duration and delivery-mode fields in CSU embedded course data.
---

For CSU, prefer `full_time_minimum_years`, then standard EFTSL, over `actual_full_time` or maximum years when deriving the portal duration.

**Why:** CSU can publish the visible international duration as a minimum/standard value while `actual_full_time` contains the longer enrolment window. Treating the latter as course length inflated a 1.5-year research master to five years.

**How to apply:** Keep actual/maximum years as fallbacks only when minimum and standard values are absent.

For CSU study mode and physical location, use FPOS offerings from the latest published session year rather than unioning every embedded year.

**Why:** CSU pages embed multiple years together. Merging them can add an older Online offering to a current international year whose structured summary is On Campus only.

**How to apply:** Select the latest valid session year for mode/location authority. Intake-month extraction may still span published years because recurring session suffixes remain useful there.