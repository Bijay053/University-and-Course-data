# Production Git ownership repair — 2 November 2026

The release's non-blocking housekeeping failures came from root ownership of
two bundle-ref directories and their twelve ref/reflog files. No Git processes
or lock files were active before repair.

Changed ownership to ubuntu:ubuntu only for `.git/refs/remotes/bundle`,
`.git/logs/refs/remotes/bundle`, and their six children each:
`notredame-speed`, `notredame-timeout`, `notredame-wayback-cache`,
`notredame-pipeline`, `notredame-provider-queue`, `notredame-final`.

All fourteen entries were fenced against the expected inventory and root owner.
Symlinks were refused. Every file's SHA-256 remained unchanged.
Private backups and read-only Git object files were intentionally untouched.
Original metadata is archived privately on production at
`/var/lib/university-portal/git-ownership-20261102/bundle-ref-metadata.tar`.

Verification succeeded:
- Fetch as ubuntu with automatic maintenance disabled for this controlled check.
- Reflog expiration dry-run with both expiration policies set to never.
- Commit-graph maintenance as ubuntu.
- Git object connectivity check.
- Byte-identical before/after ref inventory, HEAD and worktree diff.
- Both application services remained active; no restarts or queue operations.

No refs were deleted, history rewritten, objects pruned or commits pushed.
SSM inspection: `a3944994-d254-4b98-b87d-35434ceb0d74`.
SSM repair and verification: `da9e6b40-3063-4b31-a7d0-ac3d94e3cca5`.

A read-only Git metadata permission check is added to the guarded release before
fetch, executed as the repository owner. It checks writable metadata directories
and refs/reflogs while excluding operational backups and normal read-only object
files. This code is not deployed by the ownership repair.

The subsequent bootstrap enhancement reads the checker from the locally
available reviewed target, not the installed predecessor. Missing target objects
or checker cause refusal before fetch; the approved target's guarded entrypoint
must still be used when bootstrapping an older installation.
