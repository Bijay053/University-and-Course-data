"""Read-only, run as repository owner; inspect writable Git metadata only."""
import os
from pathlib import Path
import subprocess
import sys


def problems(root: Path) -> list[str]:
    errors = []
    # Do not recursively chown .git: it may contain private operational backups.
    # Read-only object files are normal; ref/reflog files require write access.
    for name in ("refs", "logs", "objects"):
        base = root / name
        if not base.exists():
            continue
        def walk_error(exc):
            errors.append(name + "/[unreadable]")
        for directory, dirs, files in os.walk(base, followlinks=False, onerror=walk_error):
            path = Path(directory)
            if path.is_symlink() or not os.access(path, os.W_OK | os.X_OK):
                errors.append(str(path.relative_to(root)))
            for item in dirs + files:
                p = path / item
                if p.is_symlink():
                    errors.append(str(p.relative_to(root)))
                elif item in files and name != "objects" and not os.access(p, os.W_OK):
                    errors.append(str(p.relative_to(root)))
    for name in ("HEAD", "config", "packed-refs", "FETCH_HEAD"):
        path = root / name
        if path.exists() and (path.is_symlink() or not os.access(path, os.W_OK)):
            errors.append(name)
    if not os.access(root, os.W_OK | os.X_OK):
        errors.append(".git")
    return errors


def main():
    try:
        root = Path(subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"], text=True,
            stderr=subprocess.DEVNULL, timeout=10).strip())
        errors = problems(root)
        if errors:
            print("Release refused: Git metadata is not writable by the repository owner.")
            print("Inspect ownership of: " + ", ".join(errors[:20]))
            print("Repair only verified metadata paths; do not recursively chown .git or delete refs.")
            return 1
        print("GIT_METADATA_PREFLIGHT_OK")
        return 0
    except (OSError, subprocess.SubprocessError):
        print("Release refused: unable to inspect Git metadata permissions.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
