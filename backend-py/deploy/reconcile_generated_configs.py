"""Safely reconcile generated university YAML collisions before a Git pull."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml


class ReleaseCollisionError(RuntimeError):
    """Raised when an incoming release would overwrite an unsafe local file."""


_GENERATED_PATH_RE = re.compile(
    r"^backend-py/scraper_config/unis/[^/]+_[0-9]+\.yaml$"
)
_RUNTIME_GENERATED_PATH_RE = re.compile(
    r"^backend-py/scraper_config/runtime_unis/[^/]+_[0-9]+\.yaml$"
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


def _git_file(repo_root: Path, revision: str, relative_path: str) -> bytes:
    return subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={repo_root}",
            "show",
            f"{revision}:{relative_path}",
        ],
        cwd=repo_root,
    )


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


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return mapping leaf paths; empty containers contribute no setting."""
    if isinstance(value, dict):
        paths: set[tuple[str, ...]] = set()
        for key, child in value.items():
            paths.update(_leaf_paths(child, (*prefix, str(key))))
        return paths
    return {prefix}


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _load_yaml_mapping_bytes(content: bytes) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _audit_generated_overlay(
    repo_root: Path, overlay: Path
) -> dict[str, Any] | None:
    """Return a redundancy proof for one currently eligible regular file."""
    if overlay.is_symlink() or not overlay.is_file():
        return None
    try:
        relative_overlay = overlay.relative_to(repo_root).as_posix()
    except ValueError:
        return None
    if not _RUNTIME_GENERATED_PATH_RE.fullmatch(relative_overlay):
        return None
    try:
        content = overlay.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    generated_relative = f"backend-py/scraper_config/unis/{overlay.name}"
    if not _is_verified_generated_stub(generated_relative, content):
        return None
    if generated_relative not in _git_paths(repo_root, "ls-files"):
        return None

    recipe = repo_root / generated_relative
    if recipe.is_symlink() or not recipe.is_file():
        return None
    overlay_config = _load_yaml_mapping(overlay)
    recipe_config = _load_yaml_mapping(recipe)
    if overlay_config is None or recipe_config is None:
        return None
    overlay_leaves = _leaf_paths(overlay_config)
    recipe_leaves = _leaf_paths(recipe_config)
    if overlay_leaves - recipe_leaves:
        return None
    return {
        "overlay": relative_overlay,
        "recipe": generated_relative,
        "overlay_sha256": _sha256(overlay),
        "recipe_sha256": _sha256(recipe),
        "superseded_leaf_paths": sorted(".".join(path) for path in overlay_leaves),
    }


def find_redundant_generated_overlays(repo_root: Path) -> list[dict[str, Any]]:
    """Report verified overlays whose setting paths all exist in tracked recipes."""
    repo_root = repo_root.resolve()
    runtime_root = repo_root / "backend-py/scraper_config/runtime_unis"
    if not runtime_root.is_dir():
        return []

    redundant: list[dict[str, Any]] = []
    for overlay in sorted(runtime_root.iterdir()):
        proof = _audit_generated_overlay(repo_root, overlay)
        if proof is not None:
            redundant.append(proof)
    return redundant


def cleanup_redundant_generated_overlays(repo_root: Path) -> list[Path]:
    """Delete only overlays that still match a fresh redundancy audit."""
    repo_root = repo_root.resolve()
    removed: list[Path] = []
    for proof in find_redundant_generated_overlays(repo_root):
        overlay = repo_root / proof["overlay"]
        fresh_proof = _audit_generated_overlay(repo_root, overlay)
        if fresh_proof == proof:
            overlay.unlink()
            removed.append(overlay)
    return removed


def reconcile_generated_config_collisions(
    repo_root: Path, target: str, manifest_path: Path
) -> list[Path]:
    """Validate collisions and preserve safe stubs until release verification."""
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    tracked_target = _git_paths(repo_root, "ls-tree", "-r", "--name-only", target)
    collisions = _git_paths(repo_root, "ls-files", "--others") & tracked_target
    runtime_root = repo_root / "backend-py/scraper_config/runtime_unis"
    backup_root = (
        repo_root
        / ".git/release-generated-config-backups"
        / manifest_path.name
    )
    moves: list[tuple[Path, Path, str, bool, str, str | None]] = []
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

        target_content = _git_file(repo_root, target, relative)
        generated_config = _load_yaml_mapping(path)
        target_config = _load_yaml_mapping_bytes(target_content)
        fully_superseded = bool(
            generated_config is not None
            and target_config is not None
            and not (_leaf_paths(generated_config) - _leaf_paths(target_config))
        )
        disposition = "deferred_delete" if fully_superseded else "overlay"
        destination = (
            backup_root / relative if fully_superseded else runtime_root / path.name
        )
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ReleaseCollisionError(
                f"Generated config preservation destination already differs: {destination}"
            )
        moves.append(
            (
                path,
                destination,
                _sha256(path),
                destination.exists(),
                disposition,
                hashlib.sha256(target_content).hexdigest(),
            )
        )

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
                "disposition": disposition,
                "target_sha256": target_digest,
            }
            for (
                source,
                destination,
                digest,
                destination_preexisting,
                disposition,
                target_digest,
            ) in moves
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    moved: list[tuple[Path, Path, str, bool, str, str | None]] = []
    try:
        for move in moves:
            source, destination, digest, destination_preexisting, _, _ = move
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.move(source, destination)
            else:
                source.unlink()
            moved.append(move)
    except Exception:
        rollback_generated_config_collisions(manifest_path)
        raise
    return [destination for _, destination, _, _, _, _ in moved]


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
            if _sha256(source) == move["sha256"]:
                continue
            if _sha256(source) == move.get("target_sha256"):
                if not destination.exists() or _sha256(destination) != move["sha256"]:
                    raise ReleaseCollisionError(
                        f"Cannot safely restore generated config from {destination}"
                    )
                if move.get("disposition") == "overlay":
                    restored.append(destination)
                    continue
                overlay = (
                    repo_root
                    / "backend-py/scraper_config/runtime_unis"
                    / source.name
                )
                if overlay.exists() and _sha256(overlay) != move["sha256"]:
                    raise ReleaseCollisionError(
                        f"Cannot safely restore generated config over {overlay}"
                    )
                overlay.parent.mkdir(parents=True, exist_ok=True)
                if not overlay.exists():
                    shutil.move(destination, overlay)
                else:
                    destination.unlink()
                restored.append(overlay)
                continue
            raise ReleaseCollisionError(
                f"Original generated config changed during rollback: {source}"
            )
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


def finalize_generated_config_collisions(manifest_path: Path) -> list[Path]:
    """Delete deferred backups only after the release has been verified."""
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_root = Path(manifest["repo_root"]).resolve()
    deferred: list[Path] = []
    for move in manifest["moves"]:
        if move.get("disposition") != "deferred_delete":
            continue
        source = repo_root / move["source"]
        destination = repo_root / move["destination"]
        if not source.exists() or _sha256(source) != move.get("target_sha256"):
            raise ReleaseCollisionError(
                f"Target recipe is not verified for finalization: {source}"
            )
        if not destination.exists() or _sha256(destination) != move["sha256"]:
            raise ReleaseCollisionError(
                f"Cannot safely finalize generated config at {destination}"
            )
        deferred.append(destination)

    removed: list[Path] = []
    for destination in deferred:
        destination.unlink()
        removed.append(destination)
    manifest_path.unlink()
    for path in sorted({path.parent for path in removed}, reverse=True):
        while path != manifest_path.parent:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--repo-root", type=Path, required=True)
    prepare.add_argument("--target", required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--manifest", type=Path, required=True)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--manifest", type=Path, required=True)
    audit = subparsers.add_parser("audit-overlays")
    audit.add_argument("--repo-root", type=Path, required=True)
    cleanup = subparsers.add_parser("cleanup-overlays")
    cleanup.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        for path in reconcile_generated_config_collisions(
            args.repo_root.resolve(), args.target, args.manifest
        ):
            print(f"Preserved verified generated config overlay: {path}")
    elif args.command == "rollback":
        for path in rollback_generated_config_collisions(args.manifest):
            print(f"Restored generated config after failed release: {path}")
    elif args.command == "finalize":
        for path in finalize_generated_config_collisions(args.manifest):
            print(f"Finalized superseded generated config: {path}")
    elif args.command == "audit-overlays":
        for proof in find_redundant_generated_overlays(args.repo_root):
            print(json.dumps(proof, sort_keys=True))
    else:
        for path in cleanup_redundant_generated_overlays(args.repo_root):
            print(f"Removed redundant generated config overlay: {path}")


if __name__ == "__main__":
    main()