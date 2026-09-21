---
name: Release cleanup paths
description: Ensuring deployment cleanup restores paused worker consumers after failures.
---

Deployment cleanup traps that restore paused worker consumers must either use absolute executable paths or explicitly return to the service directory.

**Why:** Release scripts can change their working directory before a later validation fails. A relative cleanup command can then fail silently, leaving the consumer paused and subsequent smoke jobs queued indefinitely.

**How to apply:** Make cleanup independent of the script’s current directory, and verify the consumer is restored after every aborted release before starting another attempt.

A release failure after services restart and the release revision is written is
not equivalent to a pre-checkout failure. The cleanup path may restore the
previous frontend assets while leaving Git, the API/worker processes, and the
recorded release revision on the target commit.

**Why:** Public-frontend verification failed after a successful checkout,
frontend build, service restart, health check, and release-identity proof. The
failure trap restored the frontend directory but intentionally did not reset
Git or the release revision. Reusing the original predecessor then failed the
revision fence.

**How to apply:** After any late release failure, inspect Git HEAD, the recorded
release revision, active service identities, frontend asset identity, and
consumer state independently. Use the recorded active revision as the next
predecessor; never assume the whole release rolled back because the command
exited nonzero.