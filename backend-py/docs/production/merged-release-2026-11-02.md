# Merged EC2 release — 2 November 2026

Completed at 05:55:47 UTC; independent verification passed at 05:56:31 UTC.

- Previous release: `b1b702d95fabe008905a3c0e3a4fddc22d5421a2`
- Reviewed workspace snapshot: `efd0b2711b12e9c7e4d33644f05043cacc2c7e8a`
- Deployed release: `f97332588c9c82e1fe31bc18bbcbcddd3efcba14`
- Exact tested/deployed tree: `b52c31e2745cd2bb1ab76571dae17c07b6911fa7`
- Public portal: https://portal.agentsic.com/
- Verified public frontend asset: `/assets/index-CQxa8EhJ.js`

The deployed commit joins the reviewed workspace snapshot and previous production
release as parents, preserving the reviewed tree exactly. The previous release
had identical source to their common ancestor; no production-only source was
discarded. GitHub main advanced without force. Work in progress was excluded.

## Checks

199 focused bootstrap, real-owner Git permissions, runtime prerequisite,
release identity/manifest, and HSU tests passed. One existing Pydantic
deprecation warning remained.

Production prerequisites passed before samples or checkout. The guarded release
exited zero. Both original preflight and post-restart receipts were independently
verified against the completed manifest:

`/var/lib/university-portal/release-manifests/release_dfe5eb26a4f442d58218b06333a5d682/manifest.json`

- Preflight receipt SHA-256:
  `5ccdafc20eda6a552fdf74fb8c65c619dec788b69d879f9819f0d661fa3abd04`
- Post-restart receipt SHA-256:
  `b0f18e950dc5de37706b3842f85dd9f97eb95fda9499d3944c7b3c974ee25662`

Git, API, and worker release identities matched. API, worker, and Nginx were
active; public health and frontend release marker passed. Node 24.22.1 and the
effective browser Node path were verified. Complete worker-idle checks and
empty active database counts passed; the scrape consumer was enabled.

Operator settings were byte-identical before and after. Evidence and patches:
`/var/lib/university-portal/merged-release-20261102/`.
No schema migration, bulk scrape, course approval, or HSU data rewrite was
performed by this release. The earlier HSU staged recovery remains separate.
Only ordinary guarded-release samples were run.

## SSM receipts

- Initial state: `dc552c90-294a-433f-be1f-c7f227832a8f`
- Read-only prerequisite inspection: `9b089934-7ac9-4d05-b222-00190b972823`
- Guarded release: `3742e88f-9d5d-47ac-ad53-5018b45356bc`
- Independent final verification: `81fd4c19-b2a2-4f99-a7cc-a465cc3f68c2`

The local sign-in page rendered without browser errors. The signed-in UI was
not exercised; production verification used release receipts, service identity,
public HTTP checks, and the frontend release marker.
