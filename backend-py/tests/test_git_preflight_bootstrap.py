"""Executable guarded-entrypoint tests with local Git and no production access."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def snapshot(repo):
    return {str(p.relative_to(repo)): (p.lstat().st_uid, p.lstat().st_gid,
            stat.S_IMODE(p.lstat().st_mode), hashlib.sha256(p.read_bytes()).hexdigest())
            for p in repo.rglob("*") if p.is_file()}


@pytest.mark.parametrize("case", [
    "compatible", "blocked", "missing_target", "symbolic", "wrong_predecessor",
    "missing_checker", "replacement", "installed_decoy",
])
def test_bootstrap_before_fetch_or_samples(tmp_path, case):
    if case == "blocked" and os.geteuid() == 0:
        pytest.skip("real write denial requires non-root runner")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "baseline").write_text("predecessor")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "predecessor")
    predecessor = git(repo, "rev-parse", "HEAD")
    checker = repo / "backend-py/deploy/git_metadata_preflight.py"
    checker.parent.mkdir(parents=True)
    if case != "missing_checker":
        checker.write_bytes((DEPLOY / "git_metadata_preflight.py").read_bytes())
    else:
        (repo / "new-file").write_text("no checker")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "reviewed target")
    target = git(repo, "rev-parse", "HEAD")
    if case == "replacement":
        # A replacement checker would abort. The real immutable blob must win.
        blob = git(repo, "rev-parse", f"{target}:backend-py/deploy/git_metadata_preflight.py")
        bad = subprocess.check_output(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                      input=b"raise RuntimeError('unreviewed replacement')\n").decode().strip()
        git(repo, "replace", blob, bad)
    git(repo, "checkout", "-q", predecessor)
    (repo / "backend-py/deploy").mkdir(parents=True, exist_ok=True)
    if case == "installed_decoy":
        checker.write_text("raise RuntimeError('installed code must not run')\n")
    reached = tmp_path / "after-gate"
    (repo / "backend-py/.env").write_text(f"printf passed > '{reached}'\nexit 73\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" = -u ] && [ "$2" = ubuntu ] || exit 90\nshift 2\nexec "$@"\n')
    sudo.chmod(0o755)
    git_log = tmp_path / "git-calls"
    wrapper = bin_dir / "git"
    wrapper.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> '{git_log}'
case " $* " in *" fetch "*|*" checkout "*|*" merge "*) exit 91;; esac
exec '{shutil.which("git")}' "$@"
""")
    wrapper.chmod(0o755)
    ref_dir = repo / ".git/refs"
    mode = ref_dir.stat().st_mode & 0o777
    if case == "blocked":
        ref_dir.chmod(0o500)
    if case == "missing_target":
        target = "f" * 40
    elif case == "symbolic":
        target = "HEAD"
    elif case == "wrong_predecessor":
        predecessor = target
    before = snapshot(repo)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               GUARDED_RELEASE_REPO_ROOT=str(repo),
               GUARDED_RELEASE_PYTHON=sys.executable, GUARDED_RELEASE_TEST_MODE="0")
    try:
        result = subprocess.run(["bash", str(DEPLOY / "guarded_release.sh"),
                                 predecessor, target, "123456789012"],
                                env=env, capture_output=True, text=True, timeout=15)
        assert snapshot(repo) == before
        if case in {"compatible", "replacement", "installed_decoy"}:
            assert result.returncode == 73, result.stderr
            assert reached.read_text() == "passed"
            assert "GIT_METADATA_PREFLIGHT_OK" in result.stdout
        else:
            assert result.returncode != 0
            assert not reached.exists()
            assert "Release refused" in result.stdout + result.stderr
        calls = git_log.read_text() if git_log.exists() else ""
        assert not any(f" {verb} " in f" {line} " for line in calls.splitlines()
                       for verb in ("fetch", "checkout", "merge"))
        assert "RELEASE_MANIFEST=" not in result.stdout
        assert "safe restart smoke" not in result.stdout
    finally:
        ref_dir.chmod(mode)
