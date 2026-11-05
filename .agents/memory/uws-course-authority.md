---
name: UWS catalogue and admission authority
description: UWS mixed UG/PG discovery, audience-owned tuition, and taught versus research English requirements.
---

UWS separates the award from the course H1 and labels duration as “Duration & study mode”. Start dates are campus-owned and can restrict entry to years 2/3.

**Why:** Bare H1 extraction dropped awards, and generic parsing missed full-time durations. A shared page may describe multiple awards or a longer sandwich pathway; those must not be silently combined into one qualification.

**How to apply:** Combine a single explicit award with its title, retain standard versus alternative duration evidence, and keep each campus's dates and entry restrictions together. Do not import an existing course again merely because the user says “fix this” from a different review.

Use the official mixed undergraduate/postgraduate course catalogue, and verify coverage after all university filters, not just the provider response.

**Why:** UWS's postgraduate finder is dynamic. An older undergraduate-only onboarding configuration explicitly blocked the entire postgraduate path, so a successful complete API response still lost every master's course. Subject-only headings also omit the award without being category pages.

**How to apply:** Preserve strict course-leaf URL boundaries while removing blanket postgraduate exclusions. Do not let supplementary undergraduate crawling replace the provider's full mixed catalogue.

Tuition must come from the current programme's latest published international panel. Preserve “per year of study” versus “Total cost of tuition”; a modular price is neither a whole-award fee nor an annual fee without independently verified credit requirements.

**Why:** The same pages publish Scottish, other-UK, international, older-year, and per-module prices together. Picking the first amount produces domestic tuition.

**How to apply:** Keep ambiguous campus/award alternatives and module-only tuition for review rather than selecting a smaller price or guessing annualization.

Apply the appropriate admission English profile, including component floors and course-specific exceptions.

**Why:** UWS pages repeat taught-degree, research-degree, and pre-sessional English information together. The preparatory 5.0 score is not the standard taught-degree admission requirement.

**How to apply:** Use the applicable labelled profile or verified central admission guidance. Never downgrade an explicit higher course-specific requirement to the common taught profile.
