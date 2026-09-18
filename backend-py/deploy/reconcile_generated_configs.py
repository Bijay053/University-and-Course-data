"""Safely reconcile generated university YAML collisions before a Git pull."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
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
_TRACKED_RECIPE_PATH_RE = re.compile(
    r"^backend-py/scraper_config/unis/[^/]+\.yaml$"
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


def _git_regular_file_mode(
    repo_root: Path, revision: str, relative_path: str
) -> str:
    output = _git_output(
        repo_root, "ls-tree", revision, "--", relative_path
    ).strip()
    metadata, separator, returned_path = output.partition("\t")
    fields = metadata.split()
    if (
        not separator
        or returned_path != relative_path
        or len(fields) != 3
        or fields[1] != "blob"
        or fields[0] != "100644"
    ):
        raise ReleaseCollisionError(
            f"Tracked recipe is not a regular non-executable Git file at "
            f"{revision}: {relative_path}"
        )
    return fields[0]


def _git_output(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={repo_root}", *args],
        cwd=repo_root,
        text=True,
    )


def _open_parent_fd(
    repo_root: Path, relative_path: str, *, create: bool = False
) -> tuple[int, str]:
    path = Path(relative_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseCollisionError(f"Unsafe repository path: {relative_path}")
    components = path.parts
    if not components:
        raise ReleaseCollisionError("Empty repository path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(repo_root, flags)
    try:
        for component in components[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, components[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_relative(
    repo_root: Path, relative_path: str
) -> tuple[bytes, int]:
    parent_fd, leaf = _open_parent_fd(repo_root, relative_path)
    try:
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseCollisionError(
                    f"Repository path is not a regular file: {relative_path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                content = handle.read()
            return content, stat.S_IMODE(metadata.st_mode)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ReleaseCollisionError(
            f"Cannot safely read repository path: {relative_path}"
        ) from exc
    finally:
        os.close(parent_fd)


def _relative_exists(repo_root: Path, relative_path: str) -> bool:
    try:
        parent_fd, leaf = _open_parent_fd(repo_root, relative_path)
    except (OSError, ReleaseCollisionError):
        return False
    try:
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(parent_fd)


def _write_relative_exclusively(
    repo_root: Path, relative_path: str, content: bytes, mode: int
) -> None:
    parent_fd, leaf = _open_parent_fd(
        repo_root, relative_path, create=True
    )
    temporary_leaf = ""
    descriptor = -1
    try:
        for attempt in range(128):
            temporary_leaf = f".{leaf}.release-{os.getpid()}-{attempt}"
            try:
                descriptor = os.open(
                    temporary_leaf,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    mode,
                    dir_fd=parent_fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise ReleaseCollisionError(
                f"Cannot allocate release temporary file for {relative_path}"
            )
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(descriptor, mode)
        try:
            os.link(
                temporary_leaf,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ReleaseCollisionError(
                f"Release destination was recreated concurrently: {relative_path}"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_leaf:
            try:
                os.unlink(temporary_leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _rename_relative_noreplace(
    repo_root: Path,
    source_relative: str,
    destination_relative: str,
) -> None:
    source_fd, source_leaf = _open_parent_fd(repo_root, source_relative)
    destination_fd, destination_leaf = _open_parent_fd(
        repo_root, destination_relative, create=True
    )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                source_fd,
                os.fsencode(source_leaf),
                destination_fd,
                os.fsencode(destination_leaf),
                1,  # RENAME_NOREPLACE
            )
            != 0
        ):
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise ReleaseCollisionError(
                    "Release destination was recreated concurrently: "
                    f"{destination_relative}"
                )
            raise OSError(error, os.strerror(error))
    except AttributeError as exc:
        raise ReleaseCollisionError(
            "renameat2 is required for race-safe release preservation"
        ) from exc
    finally:
        os.close(source_fd)
        os.close(destination_fd)


def _unlink_relative(repo_root: Path, relative_path: str) -> None:
    parent_fd, leaf = _open_parent_fd(repo_root, relative_path)
    try:
        os.unlink(leaf, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


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


def _tracked_worktree_changes(repo_root: Path) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    output = _git_output(repo_root, "diff", "--name-status", "--no-renames")
    for line in output.splitlines():
        status, separator, relative = line.partition("\t")
        if not separator:
            raise ReleaseCollisionError(
                f"Cannot parse tracked worktree change: {line!r}"
            )
        changes.append((status, relative))
    return changes


def _prepare_tracked_recipe_entry(
    repo_root: Path, entry: dict[str, Any]
) -> None:
    source_relative = entry["path"]
    backup_relative = entry["backup"]
    quarantine_relative = entry["quarantine"]
    source_content, _ = _read_regular_relative(repo_root, source_relative)
    if hashlib.sha256(source_content).hexdigest() != entry["operator_sha256"]:
        raise ReleaseCollisionError(
            "Tracked operator recipe changed during release preparation: "
            f"{source_relative}"
        )
    if _relative_exists(repo_root, backup_relative):
        raise ReleaseCollisionError(
            f"Tracked operator recipe backup already exists: {backup_relative}"
        )
    if _relative_exists(repo_root, quarantine_relative):
        raise ReleaseCollisionError(
            f"Tracked recipe quarantine already exists: {quarantine_relative}"
        )
    _write_relative_exclusively(
        repo_root,
        backup_relative,
        bytes.fromhex(entry["operator_content_hex"]),
        int(entry["mode"]),
    )
    backup_content, _ = _read_regular_relative(repo_root, backup_relative)
    if hashlib.sha256(backup_content).hexdigest() != entry["operator_sha256"]:
        raise ReleaseCollisionError(
            f"Tracked operator recipe backup digest failed: {backup_relative}"
        )
    source_content, _ = _read_regular_relative(repo_root, source_relative)
    if hashlib.sha256(source_content).hexdigest() != entry["operator_sha256"]:
        raise ReleaseCollisionError(
            "Tracked operator recipe changed before worktree cleanup: "
            f"{source_relative}"
        )
    _rename_relative_noreplace(
        repo_root, source_relative, quarantine_relative
    )
    try:
        quarantine_content, _ = _read_regular_relative(
            repo_root, quarantine_relative
        )
        if (
            hashlib.sha256(quarantine_content).hexdigest()
            != entry["operator_sha256"]
        ):
            if not _relative_exists(repo_root, source_relative):
                _rename_relative_noreplace(
                    repo_root, quarantine_relative, source_relative
                )
            raise ReleaseCollisionError(
                "Tracked operator recipe changed during worktree cleanup: "
                f"{source_relative}"
            )
        _write_relative_exclusively(
            repo_root,
            source_relative,
            bytes.fromhex(entry["head_content_hex"]),
            int(entry["mode"]),
        )
    except BaseException:
        if (
            not _relative_exists(repo_root, source_relative)
            and _relative_exists(repo_root, quarantine_relative)
        ):
            _rename_relative_noreplace(
                repo_root, quarantine_relative, source_relative
            )
        raise
    installed_content, _ = _read_regular_relative(repo_root, source_relative)
    if hashlib.sha256(installed_content).hexdigest() != entry["head_sha256"]:
        raise ReleaseCollisionError(
            f"Failed to materialize tracked Git recipe: {source_relative}"
        )
    _unlink_relative(repo_root, quarantine_relative)


def prepare_tracked_recipe_edits(
    repo_root: Path, target: str, manifest_path: Path
) -> list[Path]:
    """Temporarily clean validated tracked recipe edits for a guarded pull."""
    repo_root = repo_root.resolve()
    manifest_path = manifest_path.resolve()
    staged = _git_paths(repo_root, "diff", "--cached", "--name-only")
    if staged:
        raise ReleaseCollisionError(
            f"Staged repository changes block release: {sorted(staged)}"
        )

    changes = _tracked_worktree_changes(repo_root)
    unsafe = [
        relative
        for status, relative in changes
        if status != "M" or not _TRACKED_RECIPE_PATH_RE.fullmatch(relative)
    ]
    if unsafe:
        raise ReleaseCollisionError(
            f"Unknown tracked worktree changes block release: {sorted(unsafe)}"
        )

    original_head = _git_output(repo_root, "rev-parse", "HEAD").strip()
    backup_root = (
        repo_root / ".git/release-tracked-config-backups" / manifest_path.name
    )
    entries: list[dict[str, Any]] = []
    for _, relative in changes:
        operator_content, operator_mode = _read_regular_relative(
            repo_root, relative
        )
        _git_regular_file_mode(repo_root, original_head, relative)
        _git_regular_file_mode(repo_root, target, relative)
        head_content = _git_file(repo_root, original_head, relative)
        try:
            target_content = _git_file(repo_root, target, relative)
        except subprocess.CalledProcessError as exc:
            raise ReleaseCollisionError(
                f"Target release does not retain tracked recipe: {relative}"
            ) from exc
        operator_digest = hashlib.sha256(operator_content).hexdigest()
        head_digest = hashlib.sha256(head_content).hexdigest()
        if operator_digest == head_digest:
            raise ReleaseCollisionError(
                f"Tracked recipe is not actually modified: {relative}"
            )
        entries.append(
            {
                "path": relative,
                "backup": str((backup_root / relative).relative_to(repo_root)),
                "quarantine": str(
                    (backup_root / f"{relative}.worktree").relative_to(repo_root)
                ),
                "operator_sha256": operator_digest,
                "operator_content_hex": operator_content.hex(),
                "head_sha256": head_digest,
                "target_sha256": hashlib.sha256(target_content).hexdigest(),
                "head_content_hex": head_content.hex(),
                "mode": operator_mode,
            }
        )

    manifest = {
        "repo_root": str(repo_root),
        "original_head": original_head,
        "target": target,
        "entries": entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    try:
        for entry in entries:
            _prepare_tracked_recipe_entry(repo_root, entry)
    except BaseException:
        try:
            restore_tracked_recipe_edits(manifest_path)
        except BaseException:
            pass
        raise
    return [repo_root / entry["path"] for entry in entries]


def restore_tracked_recipe_edits(manifest_path: Path) -> list[Path]:
    """Restore byte-exact operator recipes after a pull or failed release."""
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_root = Path(manifest["repo_root"]).resolve()
    entries = manifest["entries"]

    restore_entries: list[dict[str, Any]] = []
    for entry in entries:
        source_relative = entry["path"]
        backup_relative = entry["backup"]
        quarantine_relative = entry["quarantine"]
        if _relative_exists(repo_root, quarantine_relative):
            raise ReleaseCollisionError(
                "Tracked recipe quarantine requires manual recovery: "
                f"{quarantine_relative}"
            )
        backup_content, _ = _read_regular_relative(
            repo_root, backup_relative
        )
        if hashlib.sha256(backup_content).hexdigest() != entry["operator_sha256"]:
            raise ReleaseCollisionError(
                "Tracked operator recipe backup cannot be verified: "
                f"{backup_relative}"
            )
        source_content, _ = _read_regular_relative(repo_root, source_relative)
        current_digest = hashlib.sha256(source_content).hexdigest()
        if current_digest == entry["operator_sha256"]:
            continue
        if current_digest not in {
            entry["head_sha256"],
            entry["target_sha256"],
        }:
            raise ReleaseCollisionError(
                f"Tracked recipe changed after release snapshot: {source_relative}"
            )
        restore_entries.append(entry)

    restored: list[Path] = []
    for entry in restore_entries:
        source_relative = entry["path"]
        backup_relative = entry["backup"]
        quarantine_relative = entry["quarantine"]
        source_content, _ = _read_regular_relative(repo_root, source_relative)
        current_digest = hashlib.sha256(source_content).hexdigest()
        if current_digest not in {
            entry["head_sha256"],
            entry["target_sha256"],
        }:
            raise ReleaseCollisionError(
                "Tracked recipe changed during release restoration: "
                f"{source_relative}"
            )
        _rename_relative_noreplace(
            repo_root, source_relative, quarantine_relative
        )
        try:
            quarantine_content, _ = _read_regular_relative(
                repo_root, quarantine_relative
            )
            if hashlib.sha256(quarantine_content).hexdigest() != current_digest:
                if not _relative_exists(repo_root, source_relative):
                    _rename_relative_noreplace(
                        repo_root, quarantine_relative, source_relative
                    )
                raise ReleaseCollisionError(
                    "Tracked recipe changed during release restoration: "
                    f"{source_relative}"
                )
            _write_relative_exclusively(
                repo_root,
                source_relative,
                bytes.fromhex(entry["operator_content_hex"]),
                int(entry["mode"]),
            )
        except BaseException:
            if (
                not _relative_exists(repo_root, source_relative)
                and _relative_exists(repo_root, quarantine_relative)
            ):
                _rename_relative_noreplace(
                    repo_root, quarantine_relative, source_relative
                )
            raise
        restored_content, _ = _read_regular_relative(
            repo_root, source_relative
        )
        if (
            hashlib.sha256(restored_content).hexdigest()
            != entry["operator_sha256"]
        ):
            raise ReleaseCollisionError(
                "Tracked operator recipe restore digest failed: "
                f"{source_relative}"
            )
        _unlink_relative(repo_root, quarantine_relative)
        source = repo_root / source_relative
        restored.append(source)
    return restored


def verify_tracked_recipe_edits(manifest_path: Path) -> list[Path]:
    """Fence post-checkout worktree state to the captured operator recipes."""
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_root = Path(manifest["repo_root"]).resolve()
    current_head = _git_output(repo_root, "rev-parse", "HEAD").strip()
    if current_head != manifest["target"]:
        raise ReleaseCollisionError(
            f"Release checkout changed after pull: {current_head}"
        )
    staged = _git_paths(repo_root, "diff", "--cached", "--name-only")
    if staged:
        raise ReleaseCollisionError(
            f"Staged repository changes appeared during release: {sorted(staged)}"
        )
    expected = {entry["path"] for entry in manifest["entries"]}
    unsafe = [
        relative
        for status, relative in _tracked_worktree_changes(repo_root)
        if status != "M" or relative not in expected
    ]
    if unsafe:
        raise ReleaseCollisionError(
            "Unknown tracked worktree changes appeared during release: "
            f"{sorted(unsafe)}"
        )
    verified: list[Path] = []
    for entry in manifest["entries"]:
        content, _ = _read_regular_relative(repo_root, entry["path"])
        if hashlib.sha256(content).hexdigest() != entry["operator_sha256"]:
            raise ReleaseCollisionError(
                "Tracked operator recipe changed after restoration: "
                f"{entry['path']}"
            )
        verified.append(repo_root / entry["path"])
    return verified


def _validate_tracked_recipe_finalization(
    repo_root: Path, entries: list[dict[str, Any]]
) -> list[Path]:
    backups: list[Path] = []
    for entry in entries:
        source_relative = entry["path"]
        backup_relative = entry["backup"]
        quarantine_relative = entry["quarantine"]
        if _relative_exists(repo_root, quarantine_relative):
            raise ReleaseCollisionError(
                "Tracked recipe quarantine cannot be finalized: "
                f"{quarantine_relative}"
            )
        source_content, _ = _read_regular_relative(repo_root, source_relative)
        if hashlib.sha256(source_content).hexdigest() != entry["operator_sha256"]:
            raise ReleaseCollisionError(
                f"Tracked operator recipe is not restored: {source_relative}"
            )
        backup_content, _ = _read_regular_relative(repo_root, backup_relative)
        if hashlib.sha256(backup_content).hexdigest() != entry["operator_sha256"]:
            raise ReleaseCollisionError(
                "Tracked operator recipe backup cannot be finalized: "
                f"{backup_relative}"
            )
        backups.append(repo_root / backup_relative)
    return backups


def finalize_tracked_recipe_edits(manifest_path: Path) -> list[Path]:
    """Commit restoration before best-effort backup garbage collection."""
    if not manifest_path.exists():
        return []
    committed_path = manifest_path.with_name(f"{manifest_path.name}.committed")
    if committed_path.exists():
        raise ReleaseCollisionError(
            f"Tracked recipe committed marker already exists: {committed_path}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo_root = Path(manifest["repo_root"]).resolve()
    entries = manifest["entries"]
    _validate_tracked_recipe_finalization(repo_root, entries)
    # Revalidate the complete set at the commit boundary. Until the atomic
    # manifest rename succeeds, every backup and the rollback manifest remain.
    backups = _validate_tracked_recipe_finalization(repo_root, entries)
    manifest_path.replace(committed_path)

    removed: list[Path] = []
    for backup in backups:
        try:
            _unlink_relative(repo_root, str(backup.relative_to(repo_root)))
        except (OSError, ReleaseCollisionError):
            continue
        removed.append(backup)
    backup_root = (
        repo_root / ".git/release-tracked-config-backups" / manifest_path.name
    )
    for path in sorted({path.parent for path in removed}, reverse=True):
        while path != backup_root.parent:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent
    if all(not _relative_exists(repo_root, entry["backup"]) for entry in entries):
        try:
            committed_path.unlink()
        except OSError:
            pass
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
    prepare_tracked = subparsers.add_parser("prepare-tracked")
    prepare_tracked.add_argument("--repo-root", type=Path, required=True)
    prepare_tracked.add_argument("--target", required=True)
    prepare_tracked.add_argument("--manifest", type=Path, required=True)
    restore_tracked = subparsers.add_parser("restore-tracked")
    restore_tracked.add_argument("--manifest", type=Path, required=True)
    finalize_tracked = subparsers.add_parser("finalize-tracked")
    finalize_tracked.add_argument("--manifest", type=Path, required=True)
    verify_tracked = subparsers.add_parser("verify-tracked")
    verify_tracked.add_argument("--manifest", type=Path, required=True)
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
    elif args.command == "prepare-tracked":
        for path in prepare_tracked_recipe_edits(
            args.repo_root.resolve(), args.target, args.manifest
        ):
            print(f"Preserved tracked operator recipe: {path}")
    elif args.command == "restore-tracked":
        for path in restore_tracked_recipe_edits(args.manifest):
            print(f"Restored tracked operator recipe: {path}")
    elif args.command == "finalize-tracked":
        for path in finalize_tracked_recipe_edits(args.manifest):
            print(f"Finalized tracked operator recipe backup: {path}")
    elif args.command == "verify-tracked":
        for path in verify_tracked_recipe_edits(args.manifest):
            print(f"Verified tracked operator recipe: {path}")
    elif args.command == "audit-overlays":
        for proof in find_redundant_generated_overlays(args.repo_root):
            print(json.dumps(proof, sort_keys=True))
    else:
        for path in cleanup_redundant_generated_overlays(args.repo_root):
            print(f"Removed redundant generated config overlay: {path}")


if __name__ == "__main__":
    main()