# Selected-retry release acceptance: blocked preflight

## Closure

Closed at the user's request after they instructed: "do it or close it".
This closes the work item, **not** the outstanding acceptance requirements.
The result is **not executed / not verified**, not a passing canary.
No follow-up tasks are being created to requeue the work the user closed.
No temporary infrastructure was provisioned, no model requests were made,
and no live course data was changed. The observations below are retained
as a historical preflight record only.

Observed on 2026-11-07 at approximately 05:20 UTC. This is **not** a passing
acceptance receipt. The selected-retry canary was not executed.

## Read-only production observation

The existing production host `i-03547b132b6aa4ffb` was inspected through the
dedicated SSM identity in `ap-south-1`. Command
`cf46409e-54ec-4475-b83a-beef9301e50d` completed successfully.

- Checkout commit: `4e60233dd7761e0bff50b78f94137b640d44cf64`.
- API and Celery services both reported `active`.
- Deployed canary source SHA-256:
  `6fbbdfc2907910be04097abae9b67b13cef11eb228f706c73b90f1e78b344080`.
- Workspace canary source SHA-256:
  `9295200c251c1ec32070d9bec338120c9fd47c29a308387fabbd00668a4cd75b`.

These source files differ. The deployed commit is not available in the local
Git object store, so local ancestry checks cannot establish whether the
workspace changes are included. Checkout identity and active services do not
prove loaded worker identity, frontend identity, or release approval.
The bounded metadata query did not return a matching release variable;
no runtime release identity is claimed.

After the operator authorized temporary test infrastructure, a second read-only
inspection completed successfully:
`485b3978-3cee-4d07-a1aa-7f7c7dbd05ca`.

- The deployed canary parameterizes only `cache_case` (`valid`, `mismatch`).
  It has no `source_path` parameterization for the ordinary transport path
  required by the current procedure.
- The deployed canary still substitutes a synthetic parser-content hash for
  `get_release_revision`; it cannot certify the actual deployed identity.
- The recent release history includes selected-retry changes, but that alone
  does not establish inclusion of the later ordinary-transport extension.
- The index was clean, but `git diff --quiet` did not report a clean tracked
  checkout. The inspection did not identify or modify those differences.
  No exact committed snapshot of the on-disk production application is claimed.

These commands read Git metadata, selected release metadata, service activity,
and source hashes only. They did not access course tables, restart services,
run a scrape, approve or publish courses, or deploy code.

## Acceptance-host discovery

Read-only STS and EC2 inventory requests used the existing production SSM and
disposable principals in `ap-south-1`. The disposable account inventory returned
no running, pending, or stopped instances. The production inventory returned no
instance with an acceptance/disposable name or ownership tag. This is a
region-scoped observation, not a claim about other accounts or regions.

The operator authorized temporary test infrastructure after the initial
preflight. No isolated acceptance host or approved release was supplied.
Provisioning was deliberately deferred when the release-content check found
the older canary, to avoid billable infrastructure that cannot yet satisfy
the acceptance prerequisites.
No resources were created or modified, so no canary teardown is claimed or
required for this preflight. No model request or model spend occurred.

## Unmet acceptance requirements

Before execution, establish approval for the exact merged release and a
separate acceptance host with verifiable disposable ownership. Verify the
release source and loaded identities there. The documented workspace harness
substitutes a synthetic release identity and labels its receipts as workspace;
running it unchanged cannot certify deployed-release acceptance.

The acceptance work remains incomplete: attempt/backoff receipts, valid-cache and
fresh-extraction cases, successful-row/progress preservation, OTP browser
completion and reload, and independently verified resource teardown have not
been demonstrated against an approved deployed release.

Do not replace the historical production evidence with a pass or reuse
workspace receipts as release acceptance. Releasing code to production was
not authorized or attempted. The next prerequisite is a release-owner-approved
merged release containing the current retry/canary changes, with an exact
verifiable source identity. A separate release process must establish that
prerequisite; this task must not deploy production code to unblock itself.
