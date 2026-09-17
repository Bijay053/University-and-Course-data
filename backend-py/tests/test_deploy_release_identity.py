"""Regression tests for the production release-identity deployment contract."""

from pathlib import Path
from argparse import Namespace
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import stat
import subprocess

import pytest

import deploy.reconcile_generated_configs as generated_configs
from deploy.safe_restart_smoke import (
    SmokeFailure,
    _main,
    persist_deployment_timing_evidence,
    recent_deployment_timing_evidence,
)
from deploy.reconcile_generated_configs import (
    ReleaseCollisionError,
    cleanup_redundant_generated_overlays,
    find_redundant_generated_overlays,
    reconcile_generated_config_collisions,
    rollback_generated_config_collisions,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = BACKEND_ROOT / "deploy"
GENERAL_ENV = "/opt/university-portal/backend-py/.env"
RELEASE_ENV = "/opt/university-portal/backend-py/.release.env"
OPENAI_ENV = "-/etc/university-portal/openai.env"
SNAPSHOT_ENV = "-/etc/university-portal/snapshot-storage.env"
DATABASE_ENV = "/etc/university-portal/database.env"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True
    ).strip()


def _collision_repo(
    tmp_path: Path, *, stub_body: str, signed: bool = True
) -> tuple[Path, str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    (repo / ".gitignore").write_text(
        "backend-py/scraper_config/runtime_unis/\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    collision = repo / "backend-py/scraper_config/unis/portable_11.yaml"
    collision.parent.mkdir(parents=True)
    collision.write_text("discovery:\n  bfs_page_budget: 9\n", encoding="utf-8")
    _git(repo, "add", str(collision.relative_to(repo)))
    _git(repo, "commit", "-qm", "incoming config")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", base)

    collision.parent.mkdir(parents=True, exist_ok=True)
    if signed:
        digest = __import__("hashlib").sha256(stub_body.encode("utf-8")).hexdigest()
        stub_body = f"# Generated-stub-sha256: {digest}\n{stub_body}"
    collision.write_text(stub_body, encoding="utf-8")
    return repo, target, collision


def _environment_files(service_name: str) -> list[str]:
    service_text = (DEPLOY_DIR / service_name).read_text(encoding="utf-8")
    return [
        line.removeprefix("EnvironmentFile=").strip()
        for line in service_text.splitlines()
        if line.startswith("EnvironmentFile=")
    ]


@pytest.mark.parametrize("service_name", ["uni-api-py.service", "uni-celery.service"])
def test_service_loads_authoritative_release_environment_last(
    service_name: str,
) -> None:
    assert _environment_files(service_name) == [
        GENERAL_ENV,
        OPENAI_ENV,
        RELEASE_ENV,
        SNAPSHOT_ENV,
        DATABASE_ENV,
    ]


def test_openai_client_is_a_declared_runtime_dependency() -> None:
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert any(line.startswith("openai>=") for line in requirements.splitlines())
    assert '"openai>=' in pyproject


def test_guarded_release_preserves_verified_generated_filename_collision(
    tmp_path: Path,
) -> None:
    body = """# Portable Test University
# Hostname: portable.edu
# Country: United States  |  Currency: USD
# Slug: portable
# Auto-generated: 2026-09-16
#
# This stub was created automatically on the first scrape of this university.
# Review and expand it to improve discovery and extraction quality.
# See scraper_config/defaults.yaml for all available options.

discovery: {}

extraction:
  fees:
    default_currency: USD
"""
    repo, target, collision = _collision_repo(
        tmp_path, stub_body=body, signed=False
    )

    reconcile_generated_config_collisions(repo, target, tmp_path / "manifest.json")

    assert not collision.exists()
    overlay = repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    assert overlay.read_text(encoding="utf-8").endswith(body)


def test_guarded_release_preserves_digest_bearing_generated_collision(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery:
  max_candidates: 77
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)

    reconcile_generated_config_collisions(repo, target, tmp_path / "manifest.json")

    assert not collision.exists()
    overlay = repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    assert overlay.read_text(encoding="utf-8").endswith(body)


def test_guarded_release_still_blocks_manually_edited_generated_collision(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery: {}
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    collision.write_text(
        collision.read_text(encoding="utf-8").replace(
            "discovery: {}", "discovery: {bfs_page_budget: 4}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ReleaseCollisionError, match="Untracked runtime file would be overwritten"
    ):
        reconcile_generated_config_collisions(repo, target, tmp_path / "manifest.json")
    assert collision.exists()


def test_guarded_release_prevalidates_all_collisions_before_moving_any(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery: {}
"""
    repo, target, verified = _collision_repo(tmp_path, stub_body=body)
    verified_content = verified.read_text(encoding="utf-8")
    verified.unlink()
    _git(repo, "checkout", "-q", target)
    unknown = repo / "backend-py/scraper_config/unis/unknown_12.yaml"
    unknown.write_text("discovery: {}\n", encoding="utf-8")
    _git(repo, "add", str(unknown.relative_to(repo)))
    _git(repo, "commit", "-qm", "incoming second config")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "HEAD~2")
    verified.parent.mkdir(parents=True, exist_ok=True)
    verified.write_text(verified_content, encoding="utf-8")
    unknown.write_text("manually_owned: true\n", encoding="utf-8")

    with pytest.raises(ReleaseCollisionError):
        reconcile_generated_config_collisions(
            repo, target, tmp_path / "manifest.json"
        )

    assert verified.exists()
    assert unknown.exists()
    assert not (repo / "backend-py/scraper_config/runtime_unis").exists()


def test_failed_release_can_restore_every_prepared_generated_collision(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery: {}
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    original = collision.read_bytes()
    manifest = tmp_path / "manifest.json"

    reconcile_generated_config_collisions(repo, target, manifest)
    assert not collision.exists()

    restored = rollback_generated_config_collisions(manifest)

    assert restored == [collision]
    assert collision.read_bytes() == original
    assert not (
        repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    ).exists()
    assert not manifest.exists()


def test_rollback_keeps_an_identical_preexisting_runtime_overlay(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery: {}
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    original = collision.read_bytes()
    overlay = repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(original)
    manifest = tmp_path / "manifest.json"

    reconcile_generated_config_collisions(repo, target, manifest)
    rollback_generated_config_collisions(manifest)

    assert collision.read_bytes() == original
    assert overlay.read_bytes() == original


def _runtime_overlay(repo: Path, name: str, body: str) -> Path:
    digest = __import__("hashlib").sha256(body.encode("utf-8")).hexdigest()
    overlay = repo / f"backend-py/scraper_config/runtime_unis/{name}"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        f"# Generated-stub-sha256: {digest}\n{body}", encoding="utf-8"
    )
    return overlay


def test_overlay_audit_reports_only_fully_superseded_verified_configs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe = repo / "backend-py/scraper_config/unis/portable_11.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(
        "discovery:\n  bfs_page_budget: 9\nextraction:\n  fees:\n    default_currency: CAD\n",
        encoding="utf-8",
    )
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked recipe")
    overlay = _runtime_overlay(
        repo,
        "portable_11.yaml",
        "# Hostname: portable.edu\n"
        "# Auto-generated: 2026-09-16\n"
        "# This stub was created automatically on the first scrape of this university.\n"
        "discovery:\n  bfs_page_budget: 3\n"
        "extraction:\n  fees:\n    default_currency: USD\n",
    )

    report = find_redundant_generated_overlays(repo)

    assert report == [
        {
            "overlay": "backend-py/scraper_config/runtime_unis/portable_11.yaml",
            "recipe": "backend-py/scraper_config/unis/portable_11.yaml",
            "overlay_sha256": __import__("hashlib").sha256(
                overlay.read_bytes()
            ).hexdigest(),
            "recipe_sha256": __import__("hashlib").sha256(
                recipe.read_bytes()
            ).hexdigest(),
            "superseded_leaf_paths": [
                "discovery.bfs_page_budget",
                "extraction.fees.default_currency",
            ],
        }
    ]
    assert overlay.exists()


def test_overlay_cleanup_keeps_generated_only_and_unknown_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe = repo / "backend-py/scraper_config/unis/portable_11.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("discovery:\n  bfs_page_budget: 9\n", encoding="utf-8")
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked recipe")
    retained = _runtime_overlay(
        repo,
        "portable_11.yaml",
        "# Hostname: portable.edu\n"
        "# Auto-generated: 2026-09-16\n"
        "# This stub was created automatically on the first scrape of this university.\n"
        "discovery:\n  bfs_page_budget: 3\n  max_candidates: 77\n",
    )
    unknown = retained.parent / "operator-notes.yaml"
    unknown.write_text("manually_owned: true\n", encoding="utf-8")

    assert cleanup_redundant_generated_overlays(repo) == []
    assert retained.exists()
    assert unknown.exists()


def test_overlay_cleanup_removes_only_redundant_verified_overlay(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe = repo / "backend-py/scraper_config/unis/portable_11.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("discovery:\n  bfs_page_budget: 9\n", encoding="utf-8")
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked recipe")
    redundant = _runtime_overlay(
        repo,
        "portable_11.yaml",
        "# Hostname: portable.edu\n"
        "# Auto-generated: 2026-09-16\n"
        "# This stub was created automatically on the first scrape of this university.\n"
        "discovery:\n  bfs_page_budget: 3\n",
    )
    unknown = redundant.parent / "portable_12.yaml"
    unknown.write_text("discovery:\n  bfs_page_budget: 3\n", encoding="utf-8")
    linked = redundant.parent / "linked_13.yaml"
    linked.symlink_to(redundant)

    assert cleanup_redundant_generated_overlays(repo) == [redundant]
    assert not redundant.exists()
    assert unknown.exists()
    assert linked.is_symlink()


def test_overlay_cleanup_rejects_symlink_replacement_after_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe = repo / "backend-py/scraper_config/unis/portable_11.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("discovery:\n  bfs_page_budget: 9\n", encoding="utf-8")
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked recipe")
    overlay = _runtime_overlay(
        repo,
        "portable_11.yaml",
        "# Hostname: portable.edu\n"
        "# Auto-generated: 2026-09-16\n"
        "# This stub was created automatically on the first scrape of this university.\n"
        "discovery:\n  bfs_page_budget: 3\n",
    )
    target = tmp_path / "same-content.yaml"
    target.write_bytes(overlay.read_bytes())
    original_find = generated_configs.find_redundant_generated_overlays

    def discover_then_replace(root: Path) -> list[dict[str, object]]:
        proofs = original_find(root)
        overlay.unlink()
        overlay.symlink_to(target)
        return proofs

    monkeypatch.setattr(
        generated_configs,
        "find_redundant_generated_overlays",
        discover_then_replace,
    )

    assert cleanup_redundant_generated_overlays(repo) == []
    assert overlay.is_symlink()


def test_overlay_cleanup_rejects_recipe_untracked_after_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe = repo / "backend-py/scraper_config/unis/portable_11.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("discovery:\n  bfs_page_budget: 9\n", encoding="utf-8")
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "tracked recipe")
    overlay = _runtime_overlay(
        repo,
        "portable_11.yaml",
        "# Hostname: portable.edu\n"
        "# Auto-generated: 2026-09-16\n"
        "# This stub was created automatically on the first scrape of this university.\n"
        "discovery:\n  bfs_page_budget: 3\n",
    )
    original_find = generated_configs.find_redundant_generated_overlays

    def discover_then_untrack(root: Path) -> list[dict[str, object]]:
        proofs = original_find(root)
        _git(repo, "rm", "--cached", "-q", str(recipe.relative_to(repo)))
        return proofs

    monkeypatch.setattr(
        generated_configs,
        "find_redundant_generated_overlays",
        discover_then_untrack,
    )

    assert cleanup_redundant_generated_overlays(repo) == []
    assert overlay.exists()


def test_release_identity_smoke_check_covers_fastapi_and_celery() -> None:
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    smoke_check = readme.split("## Release-identity smoke check", maxsplit=1)[1]
    assert "--release-identity-only" in smoke_check
    assert '--journal-since "$smoke_since"' in smoke_check
    assert "--release-identity-timeout-seconds 15" in smoke_check
    assert "exact full `RELEASE_REVISION`" in smoke_check


def test_release_identity_success_reports_only_sanitized_match_timings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    release = "a" * 40
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 0.125, "uni-celery": 1.75},
        ),
    )
    args = Namespace(
        release_identity_only=True,
        journal_since="2026-09-11T00:00:00+00:00",
        release_identity_timeout_seconds=15,
        deployment_evidence_path=tmp_path / "deployments.jsonl",
        recent_deployment_evidence=None,
    )

    asyncio.run(_main(args))

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        f"release identity passed: release={release} "
        "uni-api-py_match_elapsed_s=0.125 "
        "uni-celery_match_elapsed_s=1.750\n"
    )
    record = __import__("json").loads(
        (tmp_path / "deployments.jsonl").read_text(encoding="utf-8")
    )
    assert record["revision"] == release
    assert record["api_match_elapsed_seconds"] == 0.125
    assert record["celery_match_elapsed_seconds"] == 1.75
    assert set(record) == {
        "revision",
        "timestamp",
        "api_match_elapsed_seconds",
        "celery_match_elapsed_seconds",
    }


def test_slow_release_identity_warns_and_still_persists_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    release = "c" * 40
    evidence_path = tmp_path / "deployments.jsonl"
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 6.125, "uni-celery": 2.0},
        ),
    )

    asyncio.run(
        _main(
            Namespace(
                release_identity_only=True,
                journal_since="2026-09-11T12:29:00+00:00",
                release_identity_timeout_seconds=15,
                release_identity_warning_seconds=5,
                deployment_evidence_path=evidence_path,
                recent_deployment_evidence=None,
            )
        )
    )

    output = capsys.readouterr()
    assert output.err == "release identity warning: uni-api-py 6.125s\n"
    assert output.out.startswith("release identity passed:")
    assert evidence_path.exists()


def test_successful_identity_persists_only_sanitized_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    import json

    release = "b" * 40
    evidence_path = tmp_path / "deployments.jsonl"
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 0.25, "uni-celery": 2.5},
        ),
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.persist_deployment_timing_evidence",
        lambda path, **kwargs: persist_deployment_timing_evidence(
            path,
            **kwargs,
            recorded_at=datetime(2026, 9, 11, 12, 30, tzinfo=UTC),
        ),
    )

    asyncio.run(
        _main(
            Namespace(
                release_identity_only=True,
                journal_since="2026-09-11T12:29:00+00:00",
                release_identity_timeout_seconds=15,
                deployment_evidence_path=evidence_path,
                recent_deployment_evidence=None,
            )
        )
    )

    assert json.loads(evidence_path.read_text(encoding="utf-8")) == {
        "revision": release,
        "timestamp": "2026-09-11T12:30:00Z",
        "api_match_elapsed_seconds": 0.25,
        "celery_match_elapsed_seconds": 2.5,
    }


def test_failed_identity_does_not_persist_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "deployments.jsonl"

    def fail_identity(**_kwargs: object) -> object:
        raise SmokeFailure("release mismatch")

    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services", fail_identity
    )

    with pytest.raises(SmokeFailure, match="release mismatch"):
        asyncio.run(
            _main(
                Namespace(
                    release_identity_only=True,
                    journal_since="2026-09-11T12:29:00+00:00",
                    release_identity_timeout_seconds=15,
                    deployment_evidence_path=evidence_path,
                    recent_deployment_evidence=None,
                )
            )
        )

    assert not evidence_path.exists()


def test_recent_deployment_evidence_returns_newest_records(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    import json

    evidence_path = tmp_path / "deployments.jsonl"
    for hour in range(3):
        persist_deployment_timing_evidence(
            evidence_path,
            release=str(hour) * 40,
            api_match_elapsed_seconds=hour + 0.1,
            celery_match_elapsed_seconds=hour + 0.2,
            recorded_at=datetime(2026, 9, 11, hour, tzinfo=UTC),
        )

    asyncio.run(
        _main(
            Namespace(
                recent_deployment_evidence=2,
                deployment_evidence_path=evidence_path,
            )
        )
    )

    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert [record["revision"] for record in records] == ["2" * 40, "1" * 40]


def test_deployment_evidence_rotates_only_after_retention_boundary(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "deployments.jsonl"

    for index in range(4):
        persist_deployment_timing_evidence(
            evidence_path,
            release=str(index) * 40,
            api_match_elapsed_seconds=index,
            celery_match_elapsed_seconds=index,
            max_records=3,
        )
        records = [
            json.loads(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == min(index + 1, 3)
        assert [record["revision"] for record in records] == [
            str(value) * 40 for value in range(max(0, index - 2), index + 1)
        ]
        assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600

    recent = recent_deployment_timing_evidence(evidence_path, limit=2)
    assert [record["revision"] for record in recent] == ["3" * 40, "2" * 40]


def test_concurrent_deployment_evidence_appends_are_complete_and_retained(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "deployments.jsonl"
    releases = [f"{index:040d}" for index in range(40)]

    def append(release: str) -> None:
        persist_deployment_timing_evidence(
            evidence_path,
            release=release,
            api_match_elapsed_seconds=0.1,
            celery_match_elapsed_seconds=0.2,
            max_records=len(releases),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, releases))

    records = [
        json.loads(line)
        for line in evidence_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == len(releases)
    assert {record["revision"] for record in records} == set(releases)
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
