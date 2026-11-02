# EC2 guarded release — 2 November 2026

## Result

Completed the existing EC2 production update at 03:56:58 UTC.
Independent final verification passed at approximately 03:57:43 UTC.

- Predecessor: `ded4d17705803ad682b10fa687bb58e31b7fd908`
- Frozen, deployed target: `b1b702d95fabe008905a3c0e3a4fddc22d5421a2`
- Public portal: https://portal.agentsic.com/
- Verified public frontend asset: `/assets/index-BL5AUBfw.js`
- Production Git, recorded release, API and worker identities match the target.
- API, worker and Nginx are active; public API health passed.
- Complete worker-idle checks passed, active database counts were empty, and
  the expected worker's `scrape` consumer was enabled.

The frozen target was already on the verified production GitHub origin.
Newer workspace documentation and HSU test-only commits were not added to it.
No schema migration, HSU data correction, course approval, bulk scrape, queue
purge or job termination was performed. The ordinary guarded smoke invocations
are the only release samples.

## Approved runtime prerequisites

The user explicitly approved the server prerequisites before release:

- Installed Node 24.22.1 from the official Linux x64 distribution; its downloaded
  archive passed the published SHA-256 check.
- Updated only certifi to 2026.10.19, without unrelated Python package upgrades.
  Verified that the affected emSign ECC Root CA - G3 is absent and the unrelated
  emSign Root CA - G1 remains.
- Preserved existing systemd units and operator overlays. Added narrow Node
  security drop-ins, a stable pre-start checker outside the application checkout,
  and a last-loaded nonsecret browser-runtime environment file.
- Confirmed both restarted service processes use
  `PLAYWRIGHT_NODEJS_PATH=/usr/bin/node`.
- Chromium launched, rendered a simple page and closed through patched Node.
- Retained the previous Node binary, old/new certifi wheels and original unit
  files under the private production directory
  `/var/lib/university-portal/runtime-update-20261102`.

The initial prerequisite command stopped at a build-user checker path permission
check after installing the updates. Its cleanup restored the consumer. The
protected directory was not made public; separate checks confirmed the runtime,
trust store and browser startup before the guarded release began.

## Release acceptance evidence

Original evidence remains on production:

`/var/lib/university-portal/release-manifests/release_8ba6aa93812c4f80a254b48d7f945484/manifest.json`

The manifest has `status: completed` and `release_completed: true`.
Both original phase receipts were independently re-read and validated against
their exact retained byte hashes after completion:

| Phase | Original job | Original Celery task | Receipt SHA-256 |
|---|---|---|---|
| Preflight | `safe_restart_smoke_284044ea75844eb4b4f0a6d63cb29321` | `93979896-b80d-46c0-a20b-ac00400a285d` | `6bc7931ec829eb4b726e5126f1b7eea79eb725c41ced2762a5b77f9b449a5e2f` |
| Post-restart | `safe_restart_smoke_033a3cdd3d7c410d80e4f9e6de6261a6` | `6b343468-a581-40e7-93dd-ae0938c10543` | `9b75f42bc6e35d5b372826f0d398632f8e3f353d2d8c3608fdc637008368bd53` |

Preflight ran from 03:51:22 to 03:53:28 UTC; post-restart ran from
03:54:51 to 03:56:57 UTC. Both exited zero. These are the original samples,
not substitute receipts or later observations relabelled as acceptance.

The complete guarded release log and before/after operator-setting patches are
retained in the private runtime-update directory above. Binary patch comparison
confirmed all six existing tracked scraper-configuration edits were preserved.
The guarded generated-configuration reconciliation and finalization also passed.
The signed disposable-account rehearsal proof was validated without renewal or
bypassing the independent account check.

### SSM command references

- Initial production inspection: `2acc74b3-3821-4e88-be65-5021a01f0d1c`
- Readiness/proof/health inspection: `4f8f5e5c-e01e-499e-aa19-1f2aed5ecb23`
- Runtime prerequisite inventory: `45f5fdad-0318-4f01-914e-2ea795d0e4c0`
- Runtime prerequisite installation: `fca4133d-e28c-49c8-9809-dad52c5762a4`
- Runtime prerequisite verification: `3d9f07e6-0d90-4420-a932-078a94d24d17`
- Guarded release: `9e87a89f-7d26-407f-a9ba-c3353b3c9ae4`
- Independent final verification: `f942e040-d180-4b88-a2b0-4dc31b839680`

## Local validation

- 256 focused backend tests passed, with one existing Pydantic warning:
  release identity, manifest, sample lifecycle, HSU authority, Essex TLS,
  LSBU configuration, Surrey boundaries, certificate security and archive harness.
- Frontend TypeScript and source-alias checks passed.
- Frontend production build passed.
- Both Node security-policy tests passed.
- The local login screen rendered without browser errors. Signed-in screens
  were not visually verified; production verification used actual service
  identities, HTTP health, and release-marked public assets.

Local application source matched the frozen target; the workspace included
additional HSU tests and documentation, not extra application changes.

## Remaining non-blocking issue

Production Git automatic housekeeping reported permission-denied lock creation
under old `refs/remotes/bundle/notredame-*` references. Fetch, exact-revision
fences, fast-forward checkout and the guarded release succeeded. No Git ownership
or history cleanup was attempted as part of this release. A separate follow-up
proposes a narrowly scoped ownership investigation; another proposes detecting
runtime prerequisites before beginning future guarded releases.
