---
name: Release cleanup paths
description: Ensuring deployment cleanup restores paused worker consumers after failures.
---

Deployment cleanup traps that restore paused worker consumers must either use absolute executable paths or explicitly return to the service directory.

**Why:** Release scripts can change their working directory before a later validation fails. A relative cleanup command can then fail silently, leaving the consumer paused and subsequent smoke jobs queued indefinitely.

**How to apply:** Make cleanup independent of the script’s current directory, and verify the consumer is restored after every aborted release before starting another attempt.