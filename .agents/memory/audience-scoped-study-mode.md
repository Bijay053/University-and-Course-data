---
name: Audience-scoped study mode
description: Precedence rule for course delivery mode when domestic and international views coexist.
---

An explicitly labelled international Location or Delivery field is authoritative for study mode. If that field says only Online, do not infer a physical campus from a university default and do not let domestic-panel or page-wide campus text override it. Mixed international values (Online plus a physical campus) remain Blended.

**Why:** Some course pages keep domestic and international panels in the same HTML. Whole-page matching selected domestic/on-page campus wording, then a synthetic default location reinforced the wrong On Campus result for online-only international courses.

**How to apply:** Scope extraction to the international panel first. Preserve its source/method as authoritative evidence; block synthetic location defaults only for that scoped evidence, not for low-confidence page-wide “online” mentions.

For online/distance URL templates, a parent path is authoritative only when the
host owns that exact catalogue shape and visible course-detail evidence agrees.
Strip hidden/inert content and ignore footer/navigation text. Mandatory campus
attendance makes the course mixed-mode; optional campus events do not.

**Why:** Shared chrome and hidden templates can repeat online or campus wording,
while some distance courses require short in-person blocks and others merely
offer optional events.

**How to apply:** Pair exact host/path rules with visible course-owned headings
or labelled delivery/location values. Fail open without that structure, and
test both same-host hidden-chrome cases and required-versus-optional attendance.