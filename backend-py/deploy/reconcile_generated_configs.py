"""Safely reconcile generated university YAML collisions before a Git pull."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


class ReleaseCollisionError(RuntimeError):
    """Raised when an incoming release would overwrite an unsafe local file."""


_GENERATED_PATH_RE = re.compile(
    r"^backend-py/scraper_config/unis/[^/]+_[0-9]+\.yaml$"
)
_DIGEST_PREFIX = "# Generated-stub-sha256: "
_LEGACY_STUB_RE = re.compile(
    r"\A# [^\n]+\n"
    r"# Hostname: \S+\n"
    r"# Country: [^\n]+  \|  Currency: (?P<currency>[A-Z]{3})\n"
    r"# Slug: [a-z0-9_-]+\n"
    r"# Auto-generated: \d{4}-\d{2}-\d{2}\n"
    r"#\n"
    r"# This stub was created automatically on the first scrape of this university\.\n"
    r"# Review and expand it to improve discovery and extraction quality\.\n"
    r"# See scraper_config/defaults\.yaml for all available options\.\n"
    r"\n"
    r"discovery: \{\}\n"
    r"\n"
    r"extraction:\n"
    r"  fees:\n"
    r"    default_currency: (?P<body_currency>[A-Z]{3})\n"
    r"\Z"
)


def _git_paths(repo_root: Path, *args: str) -> set[str]:
    output = subprocess.check_output(
        ["git", "-c", f"safe.directory={repo_root}", *args],
        cwd=repo_root,
        text=True,
    )
    return set(output.splitlines())


def _is_verified_generated_stub(relative_path: str, content: str) -> bool:
    if not _GENERATED_PATH_RE.fullmatch(relative_path):
        return False

    first_line, separator, body = content.partition("\n")
    if separator and first_line.startswith(_DIGEST_PREFIX):
        claimed = first_line.removeprefix(_DIGEST_PREFIX).strip()
        return bool(
            re.fullmatch(r"[0-9a-f]{64}", claimed)
            and hashlib.sha256(body.encode("utf-8")).hexdigest() == claimed
            and "# Auto-generated:" in body
            and "This stub was created automatically" in body
        )

    legacy = _LEGACY_STUB_RE.fullmatch(content)
    return bool(
        legacy and legacy.group("currency") == legacy.group("body_currency")
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile_generated_config_collisions(
    repo_root: Path, target: str, manifest_path: Path
) -> list[Path]:
    """Validate all collisions, record rollback state, then move safe stubs."""
    tracked_target = _git_paths(repo_root, "ls-tree", "-r", "--name-only", target)
    collisions = _git_paths(repo_root, "ls-files", "--others") & tracked_target
    runtime_root = repo_root / "backend-py/scraper_config/runtime_unis"
    moves: list[tuple[Path, Path, str, bool]] = []
    unsafe: list[str] = []

    for relative in sorted(collisions):
        path = repo_root / relative
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            unsafe.append(relative)
            continue
        if not _is_verified_generated_stub(relative, content):
            unsafe.append(relative)
            continue

        destination = runtime_root / path.name
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ReleaseCollisionError(
                f"Generated config overlay already differs: {destination}"
            )
        moves.append((path, destination, _sha256(path), destination.exists()))

    if unsafe:
        raise ReleaseCollisionError(
            f"Untracked runtime file would be overwritten: {sorted(unsafe)}"
        )

    manifest = {
        "repo_root": str(repo_root),
        "moves": [
            {
                "source": str(source.relative_to(repo_root)),
                "destination": str(destination.relative_to(repo_root)),
                "sha256": digest,
                "destination_preexisting": destination_preexisting,
            }
            for source, destination, digest, destination_preexisting in moves
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    moved: list[tuple[Path, Path, str, bool]] = []
    try:
        for source, destination, digest, destination_preexisting in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.move(source, destination)
            else:
                source.unlink()
            moved.append((source, destination, digest, destination_preexisting))
    except Exception:
        rollback_generated_config_collisions(manifest_path)
        raise
    return [destination for _, destination, _, _ in moved]


def rollback_generated_config_collisions(manifest_path: Path) -> list[Path]:
    """Restore prepared stubs when a release fails before reaching its target."""
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_root = Path(manifest["repo_root"]).resolve()
    restored: list[Path] = []
    for move in reversed(manifest["moves"]):
        source = repo_root / move["source"]
        destination = repo_root / move["destination"]
        if source.exists():
            if _sha256(source) != move["sha256"]:
                raise ReleaseCollisionError(
                    f"Original generated config changed during rollback: {source}"
                )
            continue
        if not destination.exists() or _sha256(destination) != move["sha256"]:
            raise ReleaseCollisionError(
                f"Cannot safely restore generated config from {destination}"
            )
        source.parent.mkdir(parents=True, exist_ok=True)
        if move.get("destination_preexisting", False):
            shutil.copy2(destination, source)
        else:
            shutil.move(destination, source)
        restored.append(source)
    manifest_path.unlink()
    return restored


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--repo-root", type=Path, required=True)
    prepare.add_argument("--target", required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        for path in reconcile_generated_config_collisions(
            args.repo_root.resolve(), args.target, args.manifest
        ):
            print(f"Preserved verified generated config overlay: {path}")
    else:
        for path in rollback_generated_config_collisions(args.manifest):
            print(f"Restored generated config after failed release: {path}")


if __name__ == "__main__":
    main()