# Node.js and eMudhra security assessment

## Findings (23 October 2026; production inspected read-only)

| Environment | Observed runtime | Assessment |
|---|---|---|
| Replit shell and Vite child processes | Node 24.22.1 | Patched |
| Nix pnpm launcher | Hard-coded Node 24.12.0 shebang | Unpatched; use `bash scripts/pnpm-secure.sh ...` |
| Local Playwright / Patchright bundled Node | 24.13.0 / 24.15.0 | Unpatched; development now selects patched system Node through the supported `PLAYWRIGHT_NODEJS_PATH` override |
| Production root and Ubuntu build user | Node 20.20.2; pnpm 9.15.9 | End-of-life Node; upgrade before the next build |
| Production Python Playwright driver | Playwright 1.62.0 bundles Node 24.18.1 | Runtime exposure during browser-assisted scraping, not merely a build dependency |
| Python certifi, development and production before changes | 2026.7.22 | Trusts the affected emSign ECC Root CA - G3 |

Nginx serves `artifacts/university-portal/dist/public` and proxies API requests
to Python. React does not run in a Node server in production. No persistent Node
process was present at inspection, but Playwright starts its Node driver on
demand. This distinction matters: “Python backend” does not eliminate Node risk.

The official security floors are 22.23.4, 24.22.1 and 26.12.1. This project
standardizes on the tested 24 LTS branch, at least 24.22.1. Node 20 is unsupported;
absence from the supported-release CVE list is not evidence of safety.

## Advisory applicability

Node 24 fixes include DNS NUL truncation (CVE-2026-105482), TLS hostname/IP
validation bypasses (105483, 106069, 106072), Brotli memory corruption/RCE
(111497), HTTP keep-alive response handling (105481), repeated randomness in
startup snapshots/SEAs (106074), and a permission-model bypass (106073).
The Node 26-only ZIP and wildcard permission issues do not directly apply to
the observed Node 24 runtimes. No exploitation is established by this review.
Network clients, package operations and browser drivers warrant patching even
though the application is not an internet-facing Node HTTP server.

## eMudhra

Eight intermediate CA private keys were compromised. Merely relying on a
certificate's revocation is inadequate for clients that do not check it.
Node 24.22.1 updates its root bundle and blocks compromised public keys.
Certifi 2026.10.19 removes the affected root while retaining unaffected emSign
roots; locally this update is installed and verified.

The production OS's Python default trust store did **not** contain the affected
G3 root, but its certifi bundle **did**. These are separate trust stores.
The site's public TLS certificate is issued by Let's Encrypt (YE2), not eMudhra,
and HTTPS validation succeeded. No site-certificate replacement is indicated.
Browser-owned trust/revocation data and arbitrary custom CA bundles are separate:
updating Node/certifi does not certify every trust store. Keep browser and OS
security updates maintained. Never disable TLS verification as a workaround.

## Local changes and validation

- Require Node 24.22.1+ on the 24 LTS line; add a build-time version guard.
- Run the frontend's managed development/build commands through patched Node,
  rather than pnpm's old interpreter shebang. The post-merge package installation
  uses the same wrapper. Use it for manual package operations too; the
  system-provided pnpm executable itself has not been replaced.
- Keep Playwright/Patchright versions unchanged and select patched Node for
  their driver processes. Both drivers successfully launched Chromium, rendered
  a page, and closed using the patched runtime.
- Require certifi >=2026.10.19 in both dependency manifests.
- Prepare API/worker service templates with an explicit external Node driver
  path and an `ExecStartPre` security-version check.
- Add version-gate and certificate-store regression tests.

The package installer initially synchronized older lockfile versions into the
environment. Those unrelated runtime downgrades were reversed before testing.
The lockfile was narrowed back to a security-only change: certifi is the only
locked package version changed. No packages were added or removed from the lock.
The existing broad application dependency constraints remain unchanged.
`uv lock --check --offline` passed. The installed environment retains its
pre-existing application-library versions; it was already ahead of the older
lockfile before this work.

Final local validation:

- Backend regression: 6,165 passed; 48 skipped and 30 deselected by the existing
  suite configuration. The separate isolation phase passed 22 tests.
- Frontend: 296 tests passed; TypeScript checking and production build passed.
- New Node version-policy tests: 2 passed.
- New certifi trust-store test: 1 passed (run separately after suite collection).
- Both Playwright and Patchright launched Chromium successfully through the
  patched-Node wrapper without changing their package versions.
- Node HTTPS and the public production site's HTTPS validation passed.
- Development API, worker and Vite services started; the login screen rendered
  with no browser errors. Signed-in screens were not visually verified.
- Wrapper/post-merge shell syntax checks and `git diff --check` passed.

These checks found no new functional regression; they do not certify unrelated
pre-existing scraping/data-quality problems or every external certificate chain.
Overall project review separately flagged course-location-dependent repair
identity matching. That pre-existing application-logic finding is outside this
security-only change and prevents claiming whole-project release approval.

## Production prerequisites — NOT performed

Do not deploy this as a routine code-only release. The existing guarded release
does not install these OS/Python updates or replace systemd unit files.
After explicit approval, in a controlled maintenance/release:

1. Upgrade `/usr/bin/node` to patched Node 24 LTS, >=24.22.1; verify the build
   user's version too.
2. Update certifi in the production `.venv` to >=2026.10.19 without unrelated
   package upgrades; verify the affected root is absent.
3. Install/review the updated API and worker unit templates (preserving
   operator settings), reload systemd, and use the guarded restart process.
   Verify effective `PLAYWRIGHT_NODEJS_PATH` is `/usr/bin/node`, not an
   overriding value in an EnvironmentFile.
4. Test driver startup, public HTTPS, frontend build and API/worker health.
5. Release only after these checks pass and the user authorizes deployment.

Production remains unchanged and still needs the above updates.

## Sources

- https://nodejs.org/en/blog/vulnerability/october-2026-security-releases
- https://nodejs.org/en/blog/release/v24.22.1
- https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2
- https://advisories.gitlab.com/pypi/certifi/GHSA-6q48-6c7w-9gw2/
