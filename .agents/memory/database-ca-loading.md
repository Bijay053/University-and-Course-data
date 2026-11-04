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
