"""Hermetic proof pairing and release cleanup tests. No network or production."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deploy import release_manifest as manifests


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SMOKE = Path(__file__).parent / "helpers" / "release_smoke_fixture.py"
BEFORE, AFTER = "a" * 40, "b" * 40


def sample(path, phase, case="valid", revision=None):
    manifests.run_sample(path, phase, [
        sys.executable, "-B", str(SMOKE),
        "--revision", revision or (BEFORE if phase == "preflight" else AFTER),
        "--case", case,
    ])


@pytest.fixture
def manifest(tmp_path):
    return manifests.initialize(tmp_path / "proof", BEFORE, AFTER)


def read(path):
    return json.loads(path.read_text())


def test_exact_revision_pair_and_private_allowlisted_manifest(manifest):
    sample(manifest, "preflight")
    preflight = read(manifest)
    assert preflight["status"] == "preflight_passed"
    assert preflight["preflight_passed"] is True
    assert preflight["release_completed"] is False
    with pytest.raises(ValueError):
        manifests.complete(manifest)
    sample(manifest, "post_restart")
    assert read(manifest)["release_completed"] is False
    manifests.complete(manifest)
    record = read(manifest)
    assert record["status"] == "completed"
    assert record["release_completed"] is True
    assert record["predecessor_revision"] == BEFORE
    assert record["target_revision"] == AFTER
    for phase, revision in (("preflight", BEFORE), ("post_restart", AFTER)):
        ref = record["phases"][phase]["receipt"]
        receipt = manifest.parent / ref["receipt_file"]
        accepted = read(receipt)
        assert ref["sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
        assert ref["revision"] == accepted["revision"] == revision
        assert ref["runtime_job_id"] == accepted["database"]["runtime_job_id"]
        assert ref["celery_task_id"] == accepted["celery"]["task_id"]
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert manifest.parent.stat().st_mode & 0o777 == 0o700
    assert "secret-not-in-manifest" not in manifest.read_text()
    assert "private-payload-not-in-manifest" not in manifest.read_text()
    original = manifest.read_bytes()
    manifests.fail(manifest)
    assert manifest.read_bytes() == original


@pytest.mark.parametrize("phase", manifests.PHASES)
@pytest.mark.parametrize("case", [
    "failed", "stale", "future", "revision", "job", "result", "observation",
    "partial", "busy", "database", "done", "symlink", "multiple", "missing", "nonzero",
])
def test_refuses_non_original_or_unsuccessful_acceptance(manifest, phase, case):
    if phase == "post_restart":
        sample(manifest, "preflight")
    before = {p: p.read_bytes() for p in manifest.parent.rglob("*.json") if p != manifest}
    with pytest.raises(ValueError):
        sample(manifest, phase, case)
    record = read(manifest)
    assert record["status"] == "failed"
    assert record["release_completed"] is False
    assert record["preflight_passed"] is (phase == "post_restart")
    assert record["phases"][phase]["status"] == "failed"
    assert "receipt" not in record["phases"][phase]
    for path, data in before.items():
        assert path.read_bytes() == data
    with pytest.raises(ValueError):
        manifests.complete(manifest)
    with pytest.raises(ValueError):
        sample(manifest, phase)


def test_rejects_same_task_reused_between_phases(manifest):
    sample(manifest, "preflight")
    with pytest.raises(ValueError, match="reused"):
        sample(manifest, "post_restart", "reuse")


def test_no_post_restart_proof_before_preflight(manifest):
    with pytest.raises(ValueError, match="transition"):
        sample(manifest, "post_restart")
    assert not (manifest.parent / "post_restart").exists()


@pytest.mark.parametrize("phase", manifests.PHASES)
def test_completion_rechecks_both_exact_receipt_hashes(manifest, phase):
    sample(manifest, "preflight")
    sample(manifest, "post_restart")
    record = read(manifest)
    path = manifest.parent / record["phases"][phase]["receipt"]["receipt_file"]
    with path.open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="changed"):
        manifests.complete(manifest)
    manifests.fail(manifest)
    assert read(manifest)["release_completed"] is False
    assert path.exists()


def test_attempts_do_not_reuse_stale_directories(tmp_path):
    first = manifests.initialize(tmp_path, BEFORE, AFTER)
    second = manifests.initialize(tmp_path, BEFORE, AFTER)
    assert first.parent != second.parent
    sample(first, "preflight")
    with pytest.raises(ValueError):
        manifests.complete(second)


def executable(path, content):
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + content)
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("failure", [
    None, "preflight", "checkout", "restart", "post_restart", "finalize", "rollback",
    "cleanup_after_acceptance",
])
def test_guarded_release_retains_proof_through_actual_exit_cleanup(tmp_path, failure):
    """Drive the real shell entrypoint with borrowed local fixtures only."""
    repo = tmp_path / "repo"
    (repo / "backend-py").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "revision").write_text(BEFORE)
    (state / "preflight").write_text("failed" if failure == "preflight" else "valid")
    (state / "post_restart").write_text("failed" if failure in ("post_restart", "rollback") else "valid")
    smoke = tmp_path / "smoke.py"
    smoke.write_text(
        "import os, sys\n"
        f"os.execv(sys.executable, [sys.executable, '-B', {str(SMOKE)!r}, "
        f"'--state-directory', {str(state)!r}, *sys.argv[1:]])\n"
    )
    fence = executable(tmp_path / "fence", (
        'if [ "$1" = checkout ]; then\n' +
        ("exit 4\n" if failure == "checkout" else f'printf %s "$4" > "{state}/revision"\n') +
        "fi\n"
    ))
    reconciler = tmp_path / "reconcile.py"
    reconciler.write_text(
        "import pathlib, sys\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "command = sys.argv[1]\n"
        "with (state / 'reconciliation').open('a') as stream: stream.write(command + '\\n')\n"
        "if command.startswith('prepare'):\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--manifest') + 1]).write_text('{}')\n"
        + ("if command == 'rollback': raise SystemExit(8)\n"
           if failure in ("rollback", "cleanup_after_acceptance") else "")
    )
    restart = executable(tmp_path / "restart", (
        f'echo restart >> "{state}/events"\n' +
        ("exit 5\n" if failure == "restart" else "")
    ))
    finalize = executable(tmp_path / "finalize", (
        f'echo finalize >> "{state}/events"\n' +
        ("exit 6\n" if failure == "finalize" else "")
    ))
    result = subprocess.run(
        ["bash", str(DEPLOY / "guarded_release.sh"), BEFORE, AFTER, "123456789012"],
        cwd=repo, capture_output=True, text=True, timeout=30,
        env={
            **os.environ, "GUARDED_RELEASE_TEST_MODE": "1",
            "GUARDED_RELEASE_REPO_ROOT": str(repo),
            "GUARDED_RELEASE_PYTHON": sys.executable,
            "GUARDED_RELEASE_REVISION_FENCE": str(fence),
            "GUARDED_RELEASE_RECONCILER": str(reconciler),
            "GUARDED_RELEASE_RESUME_MARKER": str(state / "resumed"),
            "GUARDED_RELEASE_TEST_SMOKE": str(smoke),
            "GUARDED_RELEASE_TEST_MANIFEST_HELPER": str(DEPLOY / "release_manifest.py"),
            "GUARDED_RELEASE_MANIFEST_DIRECTORY": str(tmp_path / "proof"),
            "GUARDED_RELEASE_TEST_RESTART_HOOK": str(restart),
            "GUARDED_RELEASE_TEST_FINALIZE_HOOK": str(finalize),
        },
    )
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
    path, = (tmp_path / "proof").glob("*/manifest.json")
    record = read(path)
    assert record["release_completed"] is (failure is None)
    assert record["status"] == ("completed" if failure is None else "failed")
    assert ("DEPLOYED_RELEASE=" in result.stdout) is (failure is None)
    assert record["preflight_passed"] is (failure != "preflight")
    if failure != "preflight":
        proof = manifests.verify_phase(path, record, "preflight")
        assert proof["revision"] == BEFORE
        assert "rollback" in (state / "reconciliation").read_text()
    if failure in (None, "finalize", "cleanup_after_acceptance"):
        assert manifests.verify_phase(path, record, "post_restart")["revision"] == AFTER
    if failure in ("rollback", "cleanup_after_acceptance"):
        assert not (state / "resumed").exists()
    else:
        assert (state / "resumed").exists()
    calls = (state / "calls").read_text().splitlines()
    assert calls.count("preflight") == 1
    assert calls.count("post_restart") == (failure not in ("preflight", "checkout", "restart"))
    # Cleanup must never remove a borrowed helper or retained proof.
    assert reconciler.exists() and smoke.exists()
    assert (DEPLOY / "release_manifest.py").exists()


def test_production_order_requires_post_restart_ordinary_sample():
    source = (DEPLOY / "guarded_release.sh").read_text()
    preflight = source.index('release_sample preflight "$python_bin" -B "$smoke_helper"')
    pause = source.index("# Pause consumption")
    checkout = source.index('run_release_user "$revision_fence" \\\n  checkout')
    restart = source.index("systemctl restart uni-api-py.service uni-celery.service")
    identity = source.index("--release-identity-only", restart)
    public = source.index('--public-url "$public_url"', identity)
    sample = source.index('release_sample post_restart "$python_bin" -B deploy/safe_restart_smoke.py', public)
    finalize = source.index('"$reconciler" finalize', sample)
    ready = source.index("\nrelease_succeeded=1", finalize)
    assert preflight < pause < checkout < restart < identity < public < sample < finalize < ready
    cleanup = source[source.index("cleanup_release()"):source.index("trap cleanup_release EXIT")]
    assert cleanup.index("resume_consumers") < cleanup.index("if complete_release_evidence; then")
    assert cleanup.index("if complete_release_evidence; then") < cleanup.index('echo "DEPLOYED_RELEASE=$target"')
    assert "inspect_restart_smoke.py" not in source
