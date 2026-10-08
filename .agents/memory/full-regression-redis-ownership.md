---
name: Full regression Redis ownership
description: Prevent overlapping validation runs from stopping each other's temporary Redis
---

Avoid overlapping full scraper regression runs if they may start temporary Redis processes. Keep the managed Redis workflow running before validating, rather than relying on a test script's temporary instance.

**Why:** One run started Redis while another reused it. When the first finished, its cleanup stopped Redis and caused the second run's integration test to fail despite thousands of passing tests.

**How to apply:** Confirm the managed Redis workflow owns the service before the full regression, and run only one full suite at a time.

When acceptance explicitly forbids the live broker, do not start or restart the
managed broker to satisfy an unrelated regression gate. Use a disposable,
test-owned broker in an isolated process and disclose any configured command
that cannot run unchanged under that constraint.

**Why:** Existing integration fixtures can hardcode the shared broker address;
making that address available may expose acceptance keys to active workers.

**How to apply:** Redirect only the isolated test process's Redis connections
to its private broker, retain real concurrency assertions, and remove the
owned process/socket afterwards. Never redirect active worker connections.

A failed preflight ping does not establish ownership of the shared Redis port.
Do not shut down an endpoint merely because a test script attempted to start it.

**Why:** Validation and managed Redis can start concurrently after a workspace
resumes. The test startup can lose that race but still treat the managed broker
as its temporary dependency and stop it on exit, disconnecting active workers.

**How to apply:** Give acceptance its own address and terminate only the exact
subprocess it created. Restore the managed service separately if an older
validation script's cleanup stopped it; never reuse it for isolated acceptance.