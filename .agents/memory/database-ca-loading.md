---
name: Database CA loading
description: Explicitly load operator CA bundles rather than relying on OpenSSL environment defaults.
---

Load a configured database CA file explicitly, and propagate trust-store errors.

**Why:** OpenSSL's default trust-path handling may silently ignore a missing
`SSL_CERT_FILE`; calling `create_default_context()` alone does not prove that the
operator-selected bundle was loaded.

**How to apply:** For TLS-required database clients, pass the configured CA path
to the SSL context constructor. Never retry with certificate verification
disabled or silently substitute another trust store.

## Managed configuration failures

Use the same fail-closed managed-environment loader for application startup and
maintenance checks. A provisioned host with missing or invalid settings must
not fall back to inherited configuration.

**Why:** A separate permissive maintenance parser can accept duplicate keys or
disabled TLS even when application startup rejects them, making a successful
health check misleading.

**How to apply:** Validate the entire file before changing the environment and
before importing database clients. Report generic errors without parser input
or credential contents; retain the explicit unmanaged-development case.
