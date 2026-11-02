"""Executable guarded-entrypoint tests with local Git and no production access."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.fixture(params=["workspace", "production-2.43.0"])
def bootstrap_git(request):
    """Exercise real binaries, not a shim pretending to implement old Git."""
    binary = (shutil.which("git") if request.param == "workspace" else
              os.environ.get("BOOTSTRAP_PRODUCTION_GIT", "/usr/bin/git"))
    assert binary and Path(binary).is_file(), f"Required Git binary unavailable: {binary}"
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    if request.param == "production-2.43.0":
        assert version == "git version 2.43.0", version
    return binary, tuple(map(int, version.split()[2].split(".")[:3]))


@pytest.fixture
def missing_promisor_checker(tmp_path, bootstrap_git):
    """A disposable promisor clone with exactly one required loose blob removed.

    --no-hardlinks gives the clone its own objects. Configuring partial-clone
    metadata before removing the blob models an object promised by origin, not
    ordinary repository corruption. No real repository's objects are touched.
    """
    binary, version = bootstrap_git
    # Do not pass application credentials to disposable subprocesses (or expose
    # them in pytest's fixture repr on failure).
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
           if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def run(repo, *args, extra_env=None):
        return subprocess.run(
            [binary, "-C", str(repo), *args], env=env | (extra_env or {}),
            capture_output=True, text=True, timeout=15,
        )

    def checked(repo, *args):
        result = run(repo, *args)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    checked(origin, "init", "-q")
    checked(origin, "config", "user.name", "Fixture")
    checked(origin, "config", "user.email", "fixture@example.invalid")
    (origin / "baseline").write_text("predecessor")
    checked(origin, "add", ".")
    checked(origin, "commit", "-qm", "predecessor")
    predecessor = checked(origin, "rev-parse", "HEAD")
    checker_path = "backend-py/deploy/git_metadata_preflight.py"
    checker = origin / checker_path
    checker.parent.mkdir(parents=True)
    checker.write_bytes((DEPLOY / "git_metadata_preflight.py").read_bytes())
    checked(origin, "add", ".")
    checked(origin, "commit", "-qm", "target")
    target = checked(origin, "rev-parse", "HEAD")
    blob = checked(origin, "rev-parse", f"{target}:{checker_path}")
    repo = tmp_path / "promisor"
    checked(origin, "clone", "--no-hardlinks", "--no-checkout", str(origin), str(repo))
    checked(repo, "checkout", "-q", predecessor)
    checked(repo, "config", "core.repositoryformatversion", "1")
    checked(repo, "config", "extensions.partialClone", "origin")
    checked(repo, "config", "remote.origin.promisor", "true")
    checked(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    # This helper is entirely local and always exits before any actual I/O.
    checked(repo, "remote", "set-url", "origin", "bootstrap-sentinel::promised")
    checked(repo, "config", "protocol.bootstrap-sentinel.allow", "always")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    transport_log = tmp_path / "transport-calls"
    helper = bin_dir / "git-remote-bootstrap-sentinel"
    helper.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(transport_log))}\nexit 97\n"
    )
    helper.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    obj = repo / ".git/objects" / blob[:2] / blob[2:]
    original_obj = origin / ".git/objects" / blob[:2] / blob[2:]
    assert obj.is_file() and original_obj.is_file()
    assert obj.stat().st_ino != original_obj.stat().st_ino
    obj.unlink()  # Only the tmp_path clone's independently owned loose blob.
    assert original_obj.is_file()
    assert not list((repo / ".git/objects/pack").glob("*.pack"))
    (repo / "backend-py").mkdir(exist_ok=True)
    reached = tmp_path / "after-gate"
    (repo / "backend-py/.env").write_text(
        f"printf passed > {shlex.quote(str(reached))}\nexit 73\n"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" = -u ] && [ "$2" = ubuntu ] || exit 90\nshift 2\nexec "$@"\n')
    sudo.chmod(0o755)
    # Select the tested Git for the entrypoint, including Git's own child fetch.
    (bin_dir / "git").symlink_to(binary)
    return {
        "repo": repo, "env": env, "run": run, "version": version,
        "predecessor": predecessor, "target": target,
        "spec": f"{target}:{checker_path}", "transport_log": transport_log,
        "reached": reached,
    }


@pytest.mark.parametrize("protection", ["none", "lazy-only", "protocol-only", "both"])
def test_promisor_transport_sentinel_controls(missing_promisor_checker, protection):
    fixture = missing_promisor_checker
    extra_env = {}
    if protection in {"lazy-only", "both"}:
        extra_env["GIT_NO_LAZY_FETCH"] = "1"
    if protection in {"protocol-only", "both"}:
        extra_env["GIT_ALLOW_PROTOCOL"] = ""
    before = snapshot(fixture["repo"])
    result = fixture["run"](fixture["repo"], "show", fixture["spec"], extra_env=extra_env)
    assert result.returncode != 0
    assert snapshot(fixture["repo"]) == before
    # Distribution builds may backport GIT_NO_LAZY_FETCH (including the local
    # 2.43.0 binary). Its explicit warning distinguishes that behavior from
    # upstream older Git, which ignores the variable and invokes the sentinel.
    # The protocol-only case proves safety even without lazy-fetch suppression.
    # A child `git fetch` is not itself a transport attempt.
    lazy_disabled = "lazy fetching disabled" in result.stderr
    expects_transport = protection == "none" or (
        protection == "lazy-only" and fixture["version"] < (2, 48, 0)
        and not lazy_disabled
    )
    assert fixture["transport_log"].exists() == expects_transport, result.stderr
    if expects_transport:
        assert "origin promised" in fixture["transport_log"].read_text()


def test_bootstrap_missing_promisor_checker_never_invokes_transport(missing_promisor_checker):
    fixture = missing_promisor_checker
    before = snapshot(fixture["repo"])
    env = fixture["env"] | {
        "GUARDED_RELEASE_REPO_ROOT": str(fixture["repo"]),
        "GUARDED_RELEASE_PYTHON": sys.executable,
        "GUARDED_RELEASE_TEST_MODE": "0",
    }
    result = subprocess.run(
        ["bash", str(DEPLOY / "guarded_release.sh"), fixture["predecessor"],
         fixture["target"], "123456789012"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Release refused: reviewed target lacks the Git metadata checker." in result.stderr
    assert not fixture["transport_log"].exists(), "Bootstrap attempted a transport"
    assert not fixture["reached"].exists(), "Bootstrap sourced runtime configuration"
    assert snapshot(fixture["repo"]) == before
    assert "RELEASE_MANIFEST=" not in result.stdout
    assert "safe restart smoke" not in result.stdout


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
