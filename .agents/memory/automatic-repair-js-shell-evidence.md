---
name: Automatic repair JS-shell evidence
description: How live configuration repair should handle successful HTTP responses that contain only JavaScript verification shells.
---

Automatic configuration repair must not treat a short JavaScript-required or robot-verification document as usable HTML. When direct HTTP returns that shell, use the bounded browser pool within the existing live-evidence timeout, then apply the same strict course-owned evidence checks to the rendered result.

**Why:** A university can return HTTP 200 for every official course URL while serving only a JavaScript-disabled verification shell. Without rendered fallback, repair incorrectly reports that valid course pages are unrecognized and refuses a safe URL-filter correction.

**How to apply:** Detect shells conservatively so incidental `<noscript>` text on a real page does not trigger. Keep the fallback on the official host, preserve page/time limits, and add legitimate site labels such as “Course length” to bounded field recognition rather than weakening listing, non-degree, or ownership gates.