---
name: Wouter navigation guards
description: Handling dirty forms across in-app routing, browser history, and tab unload
---

Wouter's `aroundNav` wraps its links and `useLocation` navigation, but browser Back/Forward raises `popstate` after the history entry changes and does not call `aroundNav`. Browser reload and tab close instead require `beforeunload`, whose warning text is browser-controlled.

**Why:** Treating `aroundNav` as a universal navigation blocker leaves dirty drafts vulnerable to browser navigation even when all in-app links are covered.

**How to apply:** For future unsaved-form guards, cover Wouter navigation, browser history, and unload as separate paths. Test cancelled and accepted history transitions as well as successful save and explicit discard.