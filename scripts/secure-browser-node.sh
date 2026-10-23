#!/usr/bin/env bash
# Keep Playwright/Patchright versions unchanged; replace only their Node runtime.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
node "$root/scripts/check-node-security.cjs" >/dev/null
exec node "$@"
