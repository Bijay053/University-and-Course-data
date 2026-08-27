---
name: Required browser state validation
description: Safety rules for audience-dependent pages in pooled browser sessions and timeout-recovered DOM.
---

Audience-dependent browser fetches must verify the authoritative state before
returning HTML. Required transitions should be idempotent: accept an explicit
already-reached marker, otherwise perform the transition and require its
confirmation. Explicit terminal ineligible states may also satisfy navigation,
provided extraction rejects them before reading course fields.

Required checks apply equally to normal navigation and substantive partial DOM
recovered after a navigation timeout. Failure to confirm an allowed state must
reject the HTML.

**Why:** Pooled sessions retain audience cookies, so a later page may already be
in the desired state and no longer expose the transition control. Timeout
recovery can instead expose the default state before actions run. Both cases can
otherwise produce false failures or silently extract the wrong audience.

**How to apply:** Define allowed desired and terminal markers, make transitions
idempotent, run the same required checks on normal and timeout-recovered DOM,
and test already-reached, successful-transition, terminal, and failure paths.