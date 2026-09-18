"""The audit may evolve without silently running different application code."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/uel_isolated_catalogue_audit.py"
spec = importlib.util.spec_from_file_location("uel_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def released_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "audit-test@example.invalid")
    git(tmp_path, "config", "user.name", "Audit Test")
    source = tmp_path / "backend-py/app/main.py"
    source.parent.mkdir(parents=True)
    source.write_text("RELEASED = True\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "released application")
    return tmp_path, source, git(tmp_path, "rev-parse", "HEAD")


def test_accepts_original_release(released_repo):
    root, _, release = released_repo
    assert audit.verify_runtime_release(release, root) == release


def test_accepts_later_documentation_commit_with_identical_runtime(released_repo):
    root, _, release = released_repo
    (root / "audit.md").write_text("Verification documentation\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "add audit documentation")
    current = git(root, "rev-parse", "HEAD")
    assert current != release
    assert audit.verify_runtime_release(release, root) == current


@pytest.mark.parametrize("committed", [False, True])
def test_rejects_changed_runtime_even_if_committed(released_repo, committed):
    root, source, release = released_repo
    source.write_text("RELEASED = False\n")
    if committed:
        git(root, "add", ".")
        git(root, "commit", "-qm", "change runtime")
    with pytest.raises(SystemExit, match="runtime source/config/dependencies differ"):
        audit.verify_runtime_release(release, root)


def test_rejects_untracked_runtime_file(released_repo):
    root, source, release = released_repo
    (source.parent / "new_module.py").write_text("unexpected = True\n")
    with pytest.raises(SystemExit, match="untracked runtime files"):
        audit.verify_runtime_release(release, root)


def test_rejects_blob_identity(released_repo):
    root, _, _ = released_repo
    blob = git(root, "rev-parse", "HEAD:backend-py/app/main.py")
    with pytest.raises(SystemExit, match="does not identify a commit"):
        audit.verify_runtime_release(blob, root)


@pytest.mark.parametrize("identity", ["HEAD", "7e0673c", "--help"])
def test_rejects_non_exact_identity(identity, tmp_path):
    with pytest.raises(SystemExit, match="exact full Git commit SHA"):
        audit.verify_runtime_release(identity, tmp_path)