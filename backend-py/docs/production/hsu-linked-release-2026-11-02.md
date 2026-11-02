# HSU linked English and fee-evidence release

Production revision: `a37b1e206d8e20439278bc1f860fbfc63b975fd3`.
Successful guarded acceptance completed 2026-11-02 07:24:14 UTC;
independent verification completed at 07:24:56 UTC.

The release contains the merged exact-award HSU linked-English recovery,
HSU fee currency alias/evidence consistency correction, and their tests.
193 relevant HSU and release tests passed before deployment.
Existing LSBU transport changes remain included through production ancestry.
Unrelated workspace deployment-tool changes and evidence archival work were
not copied into this narrow release.

## Preserved interrupted attempt

The first attempt checked out the target and restarted healthy services, but
post-restart acceptance refused an unrelated university scrape. Its original
manifest remains failed, not rewritten:

`/var/lib/university-portal/release-manifests/release_b5e9454d76cb473d8f082e4c676a2cf1/manifest.json`

SSM operation: `d6f58033-461a-4488-a388-e5f3ac6963a5`.
Logs and settings backups:
`/var/lib/university-portal/hsu-linked-release-20261102/`.

The frontend was restored to the preceding release. No task was revoked,
purged, or reclassified. A subsequent read-only observation confirmed that
business work had finished, all services were healthy, scrape consumption
was restored, and active database counts were empty.

## Successful exact-revision retry

The complete guarded procedure was rerun with both predecessor and target
equal to the installed target. Both fresh preflight and post-restart samples
passed. The original interrupted receipt is not used as successful acceptance.

`/var/lib/university-portal/release-manifests/release_c495b547e791460d82ad8acb1b81ab92/manifest.json`

Logs and settings backups:
`/var/lib/university-portal/hsu-linked-retry-release-20261102/`.

Independent verification SSM: `33ee89a3-aa3a-4cc9-bc9a-f109cd0e9024`.
Confirmed API and worker revision, service activity, original sample hashes,
empty active database counts, restored scrape queue, Node security baseline,
public health and matching frontend asset `/assets/index-GYpBLKqL.js`.
Production operator settings patches were byte-identical before and after.

No existing HSU review records were rewritten or approved by this release.
New scrapes and re-extractions use the deployed recovery logic.
