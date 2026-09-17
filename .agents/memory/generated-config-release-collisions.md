---
name: Generated config release collisions
description: Safety contract for reconciling runtime-generated university YAML with newly tracked release files.
---

Only an unchanged generated university stub with verifiable integrity may be moved out of an incoming tracked-file collision. Preserve its settings as a runtime overlay below the tracked recipe, so incoming release values win conflicts while generated-only values survive. Every unknown or edited untracked file must continue to block the release. Reconciliation must be failure-atomic: validate the whole collision set first and restore prepared moves unless Git is confirmed at the target revision. A runtime overlay becomes removable only when a tracked same-name recipe contains every generated leaf path; differing values are expected because the tracked value wins. Cleanup must repeat integrity, tracked-file, path-coverage, and digest checks immediately before deletion, and must ignore symlinks and unknown files.

**Why:** Production legitimately creates ID-specific YAML stubs, but Git must never overwrite a manual runtime file during a guarded fast-forward. Weak comment-only classification cannot distinguish an untouched generated file from an operator-edited one.

**How to apply:** New generated files need a digest over their exact body. A bounded legacy recognizer may accept only the exact historical template. Record a rollback manifest before moving anything; the release exit path must restore it and keep consumers paused if restoration fails. Audit cleanup by YAML leaf-path coverage rather than value equality, and retain an overlay whenever even one generated-only path remains.