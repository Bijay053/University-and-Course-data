#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "Usage: $0 <verify|checkout> <repo-root> <predecessor-full-sha> <target-full-sha>" >&2
  exit 2
fi

mode="$1"
repo_root="$2"
predecessor="$3"
target="$4"

case "$mode" in
  verify|checkout) ;;
  *)
    echo "Mode must be verify or checkout" >&2
    exit 2
    ;;
esac
if [[ ! "$predecessor" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Predecessor must be an exact lowercase 40-character Git SHA" >&2
  exit 2
fi
if [[ ! "$target" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Target must be an exact lowercase 40-character Git SHA" >&2
  exit 2
fi

cd "$repo_root"
test "$(git rev-parse HEAD)" = "$predecessor"
git fetch origin main
test "$(git rev-parse origin/main)" = "$target"
git cat-file -e "$predecessor^{commit}"
git cat-file -e "$target^{commit}"
git merge-base --is-ancestor "$predecessor" "$target"

if [ "$mode" = checkout ]; then
  git merge --ff-only "$target"
  test "$(git rev-parse HEAD)" = "$target"
fi