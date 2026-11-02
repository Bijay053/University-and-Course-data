---
name: Git lazy-fetch backports
description: Older distribution Git builds can support lazy-fetch suppression despite their version number.
---

Do not infer support for `GIT_NO_LAZY_FETCH` solely from Git's reported version. The workspace's distribution Git 2.43.0 includes the suppression and emits a “lazy fetching disabled” warning.

**Why:** A version-based expectation of a transport attempt failed against the actual local binary because the protection was backported.

**How to apply:** Use executable positive controls and a local transport sentinel. Test protocol denial independently of lazy-fetch suppression so bootstrap refusal remains safe on older builds without the backport. Local version compatibility is not proof of the production host's exact package build.
