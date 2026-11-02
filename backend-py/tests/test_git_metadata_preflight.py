import importlib.util
from pathlib import Path

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
    assert production.index('run_release_user "$python_bin"') < production.index('run_release_user "$revision_fence" verify')
