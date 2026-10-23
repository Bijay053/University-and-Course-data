#!/usr/bin/env bash
# Nix's pnpm launcher may have an older Node baked into its shebang.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
node "$root/scripts/check-node-security.cjs" >/dev/null
exec node "$(command -v pnpm)" "$@"
