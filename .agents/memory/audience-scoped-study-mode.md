---
name: Audience-scoped study mode
description: Precedence rule for course delivery mode when domestic and international views coexist.
---

An explicitly labelled international Location or Delivery field is authoritative for study mode. If that field says only Online, do not infer a physical campus from a university default and do not let domestic-panel or page-wide campus text override it. Mixed international values (Online plus a physical campus) remain Blended.

**Why:** Some course pages keep domestic and international panels in the same HTML. Whole-page matching selected domestic/on-page campus wording, then a synthetic default location reinforced the wrong On Campus result for online-only international courses.

**How to apply:** Scope extraction to the international panel first. Preserve its source/method as authoritative evidence; block synthetic location defaults only for that scoped evidence, not for low-confidence page-wide “online” mentions.