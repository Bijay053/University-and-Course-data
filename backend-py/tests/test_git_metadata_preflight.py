import importlib.util
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys

import pytest

FILE = Path(__file__).resolve().parents[1] / "deploy/git_metadata_preflight.py"
spec = importlib.util.spec_from_file_location("git_metadata_gate", FILE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_private_backups_and_readonly_objects_are_not_repaired(tmp_path, monkeypatch):
    (tmp_path / "private-backup").mkdir()
    (tmp_path / "objects").mkdir()
    (tmp_path / "objects/blob").write_text("object")
    monkeypatch.setattr(gate.os, "access", lambda p, mode: Path(p).name not in {"private-backup", "blob"})
    assert gate.problems(tmp_path) == []


def test_unwritable_ref_directory_and_reflog_detected(tmp_path, monkeypatch):
    (tmp_path / "refs/bundle").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/ref").write_text("unchanged")
    monkeypatch.setattr(gate.os, "access", lambda p, mode: Path(p).name not in {"bundle", "ref"})
    assert set(gate.problems(tmp_path)) == {"refs/bundle", "logs/ref"}
    assert (tmp_path / "logs/ref").read_text() == "unchanged"


def test_symlink_is_not_followed(tmp_path):
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs/link").symlink_to("/nonexistent")
    assert "refs/link" in gate.problems(tmp_path)


def test_preflight_runs_as_owner_before_fetch():
    script = (FILE.parent / "guarded_release.sh").read_text()
    production = script[script.index('if [ "$release_test_mode" != 1 ]; then'):]
    assert production.index("verify_target_git_metadata") < production.index('run_release_user "$revision_fence" verify')


@pytest.fixture
def owner_repo(tmp_path):
    """Real kernel permissions; opt in, never use production or elevate."""
    if os.environ.get("RUN_GIT_OWNER_TESTS") != "1":
        pytest.skip("set RUN_GIT_OWNER_TESTS=1 for disposable Linux permission tests")
    if sys.platform != "linux" or os.geteuid() == 0:
        pytest.skip("requires a non-root Linux test runner")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    metadata = repo / ".git"
    (metadata / "refs/blocked").mkdir()
    (metadata / "logs").mkdir(exist_ok=True)
    (metadata / "logs/entry").write_text("reflog evidence\n")
    (metadata / "refs/blocked/entry").write_text("reference evidence\n")
    (metadata / "private-backup").mkdir()
    (metadata / "private-backup/secret").write_text("must not traverse")
    (metadata / "objects/read-only").write_text("immutable object fixture")
    (metadata / "objects/read-only").chmod(0o444)
    checker = tmp_path / "checker.py"
    checker.write_bytes(FILE.read_bytes())
    try:
        yield repo, checker
    finally:
        # Restore only test-owned permissions for pytest disposal, never links.
        for parent, dirs, files in os.walk(metadata, topdown=True, followlinks=False):
            for path in [Path(parent), *(Path(parent) / n for n in dirs + files)]:
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)


def owner_inspect(repo, checker):
    code = """import importlib.util,json,os,sys
from pathlib import Path
assert os.geteuid() != 0
s=importlib.util.spec_from_file_location('checker',sys.argv[1])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
print(json.dumps({'uid':os.geteuid(),'problems':m.problems(Path(sys.argv[2]))}))
"""
    result = subprocess.run([sys.executable, "-B", "-c", code, str(checker), str(repo / ".git")],
                            check=True, text=True, capture_output=True, timeout=10)
    return json.loads(result.stdout)


def fingerprint(path):
    info = path.lstat()
    content = os.readlink(path) if path.is_symlink() else (
        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), content, {
        key: os.getxattr(path, key, follow_symlinks=False)
        for key in os.listxattr(path, follow_symlinks=False)
    }


@pytest.mark.parametrize("case", ["directory", "reflog", "unreadable", "symlink", "normal"])
def test_real_owner_permissions_are_read_only(owner_repo, case):
    repo, checker = owner_repo
    root = repo / ".git"
    expected = None
    if case == "directory":
        (root / "refs/blocked").chmod(0o500)
        expected = "refs/blocked"
    elif case == "reflog":
        (root / "logs/entry").chmod(0o400)
        expected = "logs/entry"
    elif case == "unreadable":
        (root / "refs/blocked").chmod(0o000)
        expected = "refs/[unreadable]"
    elif case == "symlink":
        (root / "refs/link").symlink_to(root / "private-backup", target_is_directory=True)
        expected = "refs/link"
    # Snapshot accessible bytes first. The unreadable fixture's known bytes are
    # compared after restoring its original permissions below.
    restricted = root / "refs/blocked"
    original_mode = stat.S_IMODE(restricted.stat().st_mode)
    restricted.chmod(0o700)
    paths = [root, *root.rglob("*")]
    before = {p: fingerprint(p) for p in paths}
    restricted.chmod(original_mode)
    private = root / "private-backup"
    private.chmod(0o000)
    result = owner_inspect(repo, checker)
    cli = subprocess.run([sys.executable, "-B", str(checker)], cwd=repo,
                         capture_output=True, text=True, timeout=10)
    assert cli.returncode == (1 if expected else 0)
    assert "must not traverse" not in cli.stdout + cli.stderr
    assert restricted.stat().st_mode & 0o777 == original_mode
    assert private.stat().st_mode & 0o777 == 0
    restricted.chmod(0o700)
    private.chmod(before[private][2])
    assert {p: fingerprint(p) for p in paths} == before
    assert result["uid"] == os.geteuid()
    assert not any("private-backup" in item for item in result["problems"])
    if expected:
        assert expected in result["problems"]
    else:
        assert result["problems"] == []


def test_real_posix_acl_denial_is_preserved(owner_repo):
    repo, checker = owner_repo
    path = repo / ".git/logs/entry"
    # Linux POSIX ACL xattr format: version 2, tag/permissions/qualifier.
    # Owner denied write; named-user entry makes this a genuine extended ACL.
    entries = [(1, 4, 0xFFFFFFFF), (2, 6, os.geteuid() + 1),
               (4, 4, 0xFFFFFFFF), (16, 6, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)
    try:
        os.setxattr(path, "system.posix_acl_access", acl)
    except OSError as exc:
        if exc.errno in (95, 38):
            pytest.skip("disposable filesystem does not support POSIX ACLs")
        raise
    before = fingerprint(path)
    assert not os.access(path, os.W_OK)
    result = owner_inspect(repo, checker)
    assert "logs/entry" in result["problems"]
    assert fingerprint(path) == before
