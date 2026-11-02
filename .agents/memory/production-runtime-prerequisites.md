---
name: Production runtime prerequisites
description: Safe runtime security updates alongside an immutable guarded application release.
---

Check installed Node and Python trust-package versions before starting a code release. A successful Git fetch does not satisfy the target's runtime requirements.

**Why:** The security-baseline release required a newer Node major and certifi, but the guarded application script neither installs packages nor updates systemd configuration. Starting it first would fail after checkout.

**How to apply:** Obtain approval for the runtime maintenance, retain rollback binaries/packages, verify signed rehearsal and idle gates, and preserve existing units through narrowly scoped drop-ins. Keep their pre-start checker outside the checkout so it remains usable on the predecessor, and put the nonsecret browser-runtime environment file last without moving database credentials.

Keep private production configuration directories private during build-user checks.

**Why:** A root-readable checker beneath the protected configuration directory cannot be opened by the Ubuntu build user, even when the checker file itself is world-readable.

**How to apply:** Verify that user's actual Node binary/version using an inline assertion or a public temporary checker; do not widen directory permissions merely to test it. Verify the effective service browser-runtime environment again after guarded restart.
