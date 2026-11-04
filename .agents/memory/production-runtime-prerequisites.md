---
name: Production runtime prerequisites
description: Safe runtime security updates alongside an immutable guarded application release.
---

Match the production build's pnpm major to the workspace tooling before
interpreting a frozen-install overrides mismatch as a broken lockfile.

**Why:** Production pnpm 9 rejected unchanged dependency files with an
overrides mismatch; pinned pnpm 10 installed the same lockfile successfully
and the isolated frontend build passed. A shared lockfile format version
does not establish package-manager configuration compatibility.

**How to apply:** Compare dependency files and package-manager versions first.
Use an isolated, pinned build-user tool when possible, preserving the global
tool and lockfile. Keep user-run build tools outside private service-state
directories rather than widening those directories' permissions.

Check installed Node and Python trust-package versions before starting a code release. A successful Git fetch does not satisfy the target's runtime requirements.

**Why:** The security-baseline release required a newer Node major and certifi, but the guarded application script neither installs packages nor updates systemd configuration. Starting it first would fail after checkout.

**How to apply:** Obtain approval for the runtime maintenance, retain rollback binaries/packages, verify signed rehearsal and idle gates, and preserve existing units through narrowly scoped drop-ins. Keep their pre-start checker outside the checkout so it remains usable on the predecessor, and put the nonsecret browser-runtime environment file last without moving database credentials.

Keep private production configuration directories private during build-user checks.

**Why:** A root-readable checker beneath the protected configuration directory cannot be opened by the Ubuntu build user, even when the checker file itself is world-readable.

**How to apply:** Verify that user's actual Node binary/version using an inline assertion or a public temporary checker; do not widen directory permissions merely to test it. Verify the effective service browser-runtime environment again after guarded restart.

Never recursively change ownership of the production Git directory to fix housekeeping.

**Why:** This host also retains private operational backups within Git metadata, while read-only object files can legitimately have a different owner. The observed failure needed only ref-directory and reflog ownership repairs.

**How to apply:** Inventory active Git processes and locks, fence exact paths and owners, preserve file hashes and ref inventories, and repair only proven writable-metadata blockers.
