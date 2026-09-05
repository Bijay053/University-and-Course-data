---
name: Production deployment routing
description: Non-obvious routing constraints for deploying this project to its external AWS host.
---

Inspect the live production repository origin before choosing a push target, and
use the dedicated SSM AWS identity for remote commands rather than the default
storage identity.

**Why:** A previously documented repository target returned “Repository not
found” while the live host and workspace both pointed to a different current
origin. The default AWS identity could access storage but was denied
`ssm:SendCommand`; the dedicated SSM identity worked.

**How to apply:** Before each external production release, read the production
HEAD, working tree, and sanitized origin as the repository owner. Push to that
verified origin, then run commands through the dedicated SSM identity. Never
print credentials, signed URLs, or credential-bearing remotes.