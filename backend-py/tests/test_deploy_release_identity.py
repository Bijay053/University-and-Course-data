"""Regression tests for the production release-identity deployment contract."""

from pathlib import Path
from argparse import Namespace
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

import deploy.reconcile_generated_configs as generated_configs
import deploy.post_checkout_overlay_audit as post_checkout_audit
from deploy.verify_frontend_release import (
    hashed_script_sources,
    verify_local_build,
    verify_public_build,
)
from deploy.safe_restart_smoke import (
    MAX_OVERLAY_AUDIT_EVIDENCE_BYTES,
    SmokeFailure,
    _main,
    persist_deployment_timing_evidence,
    read_overlay_audit_evidence,
    recent_deployment_timing_evidence,
)
from deploy.reconcile_generated_configs import (
    ReleaseCollisionError,
    cleanup_redundant_generated_overlays,
    finalize_tracked_recipe_edits,
    finalize_generated_config_collisions,
    find_redundant_generated_overlays,
    prepare_tracked_recipe_edits,
    reconcile_generated_config_collisions,
    restore_tracked_recipe_edits,
    rollback_generated_config_collisions,
    verify_tracked_recipe_edits,
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


def _release_revision_fence(
    mode: str, repo: Path, predecessor: str, target: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(DEPLOY_DIR / "release_revision_fence.sh"),
            mode,
            str(repo),
            predecessor,
            target,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _release_revision_gate(
    repo: Path, predecessor: str, target: str
) -> subprocess.CompletedProcess[str]:
    return _release_revision_fence("checkout", repo, predecessor, target)


def _release_revision_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "release-test@example.invalid")
    _git(source, "config", "user.name", "Release Test")
    (source / "release.txt").write_text("predecessor\n", encoding="utf-8")
    _git(source, "add", "release.txt")
    _git(source, "commit", "-qm", "predecessor")
    predecessor = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "-M", "main")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-qu", "origin", "main")
    (source / "release.txt").write_text("target\n", encoding="utf-8")
    _git(source, "commit", "-qam", "target")
    target = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(remote), str(checkout)],
        check=True,
    )
    _git(checkout, "checkout", "-q", predecessor)
    return checkout, source, predecessor, target


def test_guarded_release_requires_exact_revision_arguments() -> None:
    entrypoint = DEPLOY_DIR / "guarded_release.sh"
    script = entrypoint.read_text(encoding="utf-8")
    fence = DEPLOY_DIR / "release_revision_fence.sh"
    fence_script = fence.read_text(encoding="utf-8")

    assert entrypoint.stat().st_mode & stat.S_IXUSR
    assert fence.stat().st_mode & stat.S_IXUSR
    assert 'if [ "$#" -ne 3 ]' in script
    assert '[[ ! "$predecessor" =~ ^[0-9a-f]{40}$ ]]' in fence_script
    assert '[[ ! "$target" =~ ^[0-9a-f]{40}$ ]]' in fence_script
    assert "__TARGET_RELEASE__" not in script
    assert "__EXPECTED_DISPOSABLE_ACCOUNT__" not in script
    assert "release_revision_fence.sh" in script
    assert script.count('run_release_user "$revision_fence"') == 4
    assert "sudo -u ubuntu backend-py/deploy/release_revision_fence.sh" not in script
    assert '--expected-rehearsal-account-id "$expected_disposable_account"' in script


def test_release_revision_fence_has_no_production_side_effects() -> None:
    script = (DEPLOY_DIR / "release_revision_fence.sh").read_text(encoding="utf-8")

    for forbidden in ("systemctl", "safe_restart_smoke", "aws ", "cleanup", "rm "):
        assert forbidden not in script


@pytest.mark.parametrize("bad_ref", ["HEAD", "main", "0123456789ab"])
def test_release_revision_gate_rejects_symbolic_and_abbreviated_refs(
    tmp_path: Path, bad_ref: str
) -> None:
    checkout, _, predecessor, target = _release_revision_repo(tmp_path)

    assert _release_revision_gate(checkout, bad_ref, target).returncode != 0
    assert _release_revision_gate(checkout, predecessor, bad_ref).returncode != 0
    assert _git(checkout, "rev-parse", "HEAD") == predecessor


def test_release_revision_gate_rejects_non_ancestor_target(tmp_path: Path) -> None:
    checkout, source, predecessor, _ = _release_revision_repo(tmp_path)
    _git(source, "checkout", "-q", "--orphan", "unrelated")
    _git(source, "rm", "-q", "-rf", ".")
    (source / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(source, "add", "unrelated.txt")
    _git(source, "commit", "-qm", "unrelated")
    unrelated = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "--force", "origin", "unrelated:main")

    result = _release_revision_gate(checkout, predecessor, unrelated)

    assert result.returncode != 0
    assert _git(checkout, "rev-parse", "HEAD") == predecessor


def test_release_aborts_on_remote_advance_then_retries_exact_new_tip(
    tmp_path: Path,
) -> None:
    checkout, source, predecessor, stale_target = _release_revision_repo(tmp_path)
    (source / "release.txt").write_text("advanced\n", encoding="utf-8")
    _git(source, "checkout", "-q", "main")
    (source / "release.txt").write_text("advanced\n", encoding="utf-8")
    _git(source, "commit", "-qam", "advanced target")
    new_target = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "main")

    stale_result = _release_revision_gate(checkout, predecessor, stale_target)

    assert stale_result.returncode != 0
    assert _git(checkout, "rev-parse", "HEAD") == predecessor
    retry_result = _release_revision_gate(checkout, predecessor, new_target)
    assert retry_result.returncode == 0, retry_result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == new_target


def test_release_refetch_blocks_remote_advance_between_verify_and_checkout(
    tmp_path: Path,
) -> None:
    checkout, source, predecessor, reviewed_target = _release_revision_repo(tmp_path)

    verify_result = _release_revision_fence(
        "verify", checkout, predecessor, reviewed_target
    )
    assert verify_result.returncode == 0, verify_result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == predecessor

    _git(source, "checkout", "-q", "main")
    (source / "release.txt").write_text("advanced during preparation\n", encoding="utf-8")
    _git(source, "commit", "-qam", "advance during release preparation")
    newly_reviewed_target = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "main")

    stale_checkout = _release_revision_fence(
        "checkout", checkout, predecessor, reviewed_target
    )
    assert stale_checkout.returncode != 0
    assert _git(checkout, "rev-parse", "HEAD") == predecessor

    retry_result = _release_revision_fence(
        "checkout", checkout, predecessor, newly_reviewed_target
    )
    assert retry_result.returncode == 0, retry_result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == newly_reviewed_target


def test_late_revision_rejection_restores_all_prepared_release_files(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "release-test@example.invalid")
    _git(source, "config", "user.name", "Release Test")
    recipe_relative = Path("backend-py/scraper_config/unis/portable.yaml")
    collision_relative = Path(
        "backend-py/scraper_config/unis/generated_11.yaml"
    )
    recipe = source / recipe_relative
    recipe.parent.mkdir(parents=True)
    recipe.write_text("discovery:\n  bfs_page_budget: 3\n", encoding="utf-8")
    (source / ".gitignore").write_text(
        "backend-py/scraper_config/runtime_unis/\n", encoding="utf-8"
    )
    _git(source, "add", ".gitignore", str(recipe_relative))
    _git(source, "commit", "-qm", "predecessor")
    predecessor = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "-M", "main")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-qu", "origin", "main")

    recipe.write_text("discovery:\n  bfs_page_budget: 5\n", encoding="utf-8")
    collision = source / collision_relative
    collision.write_text("discovery:\n  bfs_page_budget: 7\n", encoding="utf-8")
    _git(source, "add", str(recipe_relative), str(collision_relative))
    _git(source, "commit", "-qm", "reviewed target")
    reviewed_target = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "main")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "main", str(remote), str(checkout)],
        check=True,
    )
    _git(checkout, "checkout", "-q", predecessor)
    checkout_recipe = checkout / recipe_relative
    operator_recipe = b"discovery:\n  bfs_page_budget: 9\n"
    checkout_recipe.write_bytes(operator_recipe)
    generated_body = (
        "# Hostname: generated.edu\n"
        "# Auto-generated: 2026-09-21\n"
        "# This stub was created automatically on the first scrape of this university.\n"
        "discovery:\n  bfs_page_budget: 4\n"
    )
    digest = __import__("hashlib").sha256(
        generated_body.encode("utf-8")
    ).hexdigest()
    checkout_collision = checkout / collision_relative
    checkout_collision.write_text(
        f"# Generated-stub-sha256: {digest}\n{generated_body}",
        encoding="utf-8",
    )
    original_generated = checkout_collision.read_bytes()

    resume_marker = tmp_path / "resume.log"
    generated_manifest = tmp_path / "generated-manifest.json"
    tracked_manifest = tmp_path / "tracked-manifest.json"
    advance_hook = tmp_path / "advance-remote.sh"
    advance_hook.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"cd {source}\n"
        "printf 'advanced during preparation\\n' > release.txt\n"
        "git add release.txt\n"
        "git commit -qm 'advance after verification'\n"
        "git push -q origin main\n",
        encoding="utf-8",
    )
    advance_hook.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "GUARDED_RELEASE_TEST_MODE": "1",
            "GUARDED_RELEASE_REPO_ROOT": str(checkout),
            "GUARDED_RELEASE_PYTHON": sys.executable,
            "GUARDED_RELEASE_REVISION_FENCE": str(
                DEPLOY_DIR / "release_revision_fence.sh"
            ),
            "GUARDED_RELEASE_RECONCILER": str(
                DEPLOY_DIR / "reconcile_generated_configs.py"
            ),
            "GUARDED_RELEASE_RESUME_MARKER": str(resume_marker),
            "GUARDED_RELEASE_BEFORE_CHECKOUT_HOOK": str(advance_hook),
            "GUARDED_RELEASE_RECONCILIATION_MANIFEST": str(generated_manifest),
            "GUARDED_RELEASE_TRACKED_MANIFEST": str(tracked_manifest),
        }
    )
    stale_checkout = subprocess.run(
        [
            str(DEPLOY_DIR / "guarded_release.sh"),
            predecessor,
            reviewed_target,
            "123456789012",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert stale_checkout.returncode != 0

    assert _git(checkout, "rev-parse", "HEAD") == predecessor
    assert checkout_recipe.read_bytes() == operator_recipe
    assert checkout_collision.read_bytes() == original_generated
    assert resume_marker.read_text(encoding="utf-8") == "resume-attempted\n"
    assert not generated_manifest.exists()
    assert not tracked_manifest.exists()
    assert not list(checkout.rglob("*.release-backup-*"))
    assert not (
        checkout
        / "backend-py/scraper_config/runtime_unis/generated_11.yaml"
    ).exists()


def test_guarded_release_does_not_resume_after_cleanup_failure(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "checkout"
    (repo / "backend-py").mkdir(parents=True)
    resume_marker = tmp_path / "resume.log"
    generated_manifest = tmp_path / "generated-manifest.json"
    tracked_manifest = tmp_path / "tracked-manifest.json"
    fake_fence = tmp_path / "revision-fence.sh"
    fake_fence.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = verify ]; then exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_fence.chmod(0o755)
    fake_reconciler = tmp_path / "reconciler.py"
    fake_reconciler.write_text(
        "import pathlib, sys\n"
        "command = sys.argv[1]\n"
        "manifest = pathlib.Path(sys.argv[sys.argv.index('--manifest') + 1])\n"
        "if command in {'prepare', 'prepare-tracked'}:\n"
        "    manifest.write_text('{}')\n"
        "elif command == 'rollback':\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "GUARDED_RELEASE_TEST_MODE": "1",
            "GUARDED_RELEASE_REPO_ROOT": str(repo),
            "GUARDED_RELEASE_PYTHON": sys.executable,
            "GUARDED_RELEASE_REVISION_FENCE": str(fake_fence),
            "GUARDED_RELEASE_RECONCILER": str(fake_reconciler),
            "GUARDED_RELEASE_RESUME_MARKER": str(resume_marker),
            "GUARDED_RELEASE_RECONCILIATION_MANIFEST": str(generated_manifest),
            "GUARDED_RELEASE_TRACKED_MANIFEST": str(tracked_manifest),
        }
    )

    result = subprocess.run(
        [
            str(DEPLOY_DIR / "guarded_release.sh"),
            "1" * 40,
            "2" * 40,
            "123456789012",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "consumers remain paused and manifests are retained" in result.stderr
    assert generated_manifest.exists()
    assert tracked_manifest.exists()
    assert not resume_marker.exists()


def test_guarded_release_refetches_tip_before_checkout() -> None:
    script = (DEPLOY_DIR / "guarded_release.sh").read_text(encoding="utf-8")
    prepare = script.index('"$reconciler" prepare \\\n')
    checkout = script.index('"$revision_fence" \\\n  checkout', prepare)
    restart = script.index("systemctl restart uni-api-py.service uni-celery.service")

    assert prepare < checkout < restart
    assert "git pull" not in script


def test_frontend_release_verifier_requires_hashed_asset_with_target_marker(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    source = "/assets/index-AbCd1234.js"
    (dist / "index.html").write_text(
        f'<script type="module" src="{source}"></script>', encoding="utf-8"
    )
    marker = b"UNIVERSITY_PORTAL_RELEASE:" + b"1" * 40
    (assets / "index-AbCd1234.js").write_bytes(b"code;" + marker)

    assert hashed_script_sources((dist / "index.html").read_text()) == [source]
    assert verify_local_build(dist, marker) == [source]

    (assets / "index-AbCd1234.js").write_bytes(b"stale code")
    with pytest.raises(AssertionError, match="marker is absent"):
        verify_local_build(dist, marker)


def test_frontend_release_verifier_requires_public_html_and_asset_to_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    source = "/assets/index-AbCd1234.js"
    (dist / "index.html").write_text(
        f'<script type="module" src="{source}"></script>', encoding="utf-8"
    )
    marker = b"UNIVERSITY_PORTAL_RELEASE:" + b"2" * 40
    (assets / "index-AbCd1234.js").write_bytes(marker)
    responses = {
        "https://portal.example/": (
            f'<script type="module" src="{source}"></script>'.encode()
        ),
        f"https://portal.example{source}": b"public code;" + marker,
    }

    def fake_fetch(url: str) -> bytes:
        return responses[url.split("?", 1)[0]]

    monkeypatch.setattr("deploy.verify_frontend_release._fetch", fake_fetch)
    assert verify_public_build(dist, "https://portal.example/", marker) == [source]

    responses["https://portal.example/"] = (
        b'<script type="module" src="/assets/index-Stale999.js"></script>'
    )
    with pytest.raises(AssertionError, match="does not reference"):
        verify_public_build(dist, "https://portal.example/", marker)


def test_guarded_release_builds_and_verifies_frontend_before_success() -> None:
    script = (DEPLOY_DIR / "guarded_release.sh").read_text(encoding="utf-8")

    stage = script.index('frontend_stage="$(mktemp -d')
    ownership = script.index('chown ubuntu:ubuntu "$frontend_stage"', stage)
    build = script.index("VITE_RELEASE_MARKER=")
    assert 'run_release_user env \\\n  VITE_RELEASE_MARKER=' in script
    assert 'run build \\\n    --outDir "$frontend_stage"' in script
    assert "run build -- \\" not in script
    assert stage < ownership < build
    publish_started = script.index("frontend_publish_started=1", build)
    move_previous = script.index('mv "$frontend_dist" "$frontend_previous"', build)
    previous_moved = script.index("frontend_previous_moved=1", move_previous)
    published = script.index("frontend_published=1", previous_moved)
    publish = script.index('mv "$frontend_stage" "$frontend_dist"', build)
    restart = script.index("systemctl restart uni-api-py.service uni-celery.service")
    public_verify = script.index('--public-url "$public_url"', restart)
    success = script.index('echo "DEPLOYED_RELEASE=$target"')

    assert (
        build
        < publish_started
        < move_previous
        < previous_moved
        < published
        < publish
        < restart
        < public_verify
        < success
    )
    assert 'if [ "$frontend_publish_started" = 1 ]' in script
    assert 'if [ "$frontend_published" = 1 ]' in script
    assert '[ "$frontend_previous_moved" = 1 ]' in script
    assert 'mv "$frontend_previous" "$frontend_dist"' in script


def test_public_release_fetch_uses_browser_compatible_user_agent(
    monkeypatch,
) -> None:
    from deploy import verify_frontend_release

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout=b"ok")

    monkeypatch.setattr(verify_frontend_release.subprocess, "run", fake_run)
    assert verify_frontend_release._fetch("https://portal.example/") == b"ok"
    assert captured["command"][-2:] == [
        "Mozilla/5.0 UniversityPortalReleaseVerifier/1.0",
        "https://portal.example/",
    ]
    assert captured["kwargs"] == {
        "check": True,
        "capture_output": True,
    }


def test_public_release_fetch_retries_transient_edge_failure(monkeypatch) -> None:
    from deploy import verify_frontend_release

    attempts = 0

    def fake_run(command, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(22, command)
        return subprocess.CompletedProcess(command, 0, stdout=b"ready")

    sleeps = []
    monkeypatch.setattr(verify_frontend_release.subprocess, "run", fake_run)
    monkeypatch.setattr(verify_frontend_release.time, "sleep", sleeps.append)

    assert verify_frontend_release._fetch("https://portal.example/") == b"ready"
    assert attempts == 2
    assert sleeps == [5]


def test_frontend_cleanup_restores_previous_dist_after_publish_move_failure(
    tmp_path: Path,
) -> None:
    script = (DEPLOY_DIR / "guarded_release.sh").read_text(encoding="utf-8")
    function_start = script.index("cleanup_frontend_release() {")
    function_end = script.index("\n}\ncleanup_release()", function_start) + 2
    cleanup_function = script[function_start:function_end]
    frontend_dist = tmp_path / "dist" / "public"
    previous = tmp_path / "previous"
    stage = tmp_path / "stage"
    previous.mkdir()
    stage.mkdir()
    (previous / "index.html").write_text("previous release", encoding="utf-8")
    (stage / "index.html").write_text("new release", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"{cleanup_function}\n"
                f"frontend_dist={frontend_dist!s}\n"
                f"frontend_previous={previous!s}\n"
                f"frontend_stage={stage!s}\n"
                "frontend_publish_started=1\n"
                "frontend_previous_moved=1\n"
                "frontend_published=1\n"
                "release_succeeded=0\n"
                "cleanup_frontend_release\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (frontend_dist / "index.html").read_text() == "previous release"
    assert not previous.exists()
    assert not stage.exists()


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

def test_guarded_release_defers_fully_superseded_collision_without_overlay(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery:
  bfs_page_budget: 3
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    manifest = tmp_path / "manifest.json"
    original = collision.read_bytes()

    preserved = reconcile_generated_config_collisions(repo, target, manifest)

    assert not collision.exists()
    assert not (
        repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    ).exists()
    assert preserved[0].read_bytes() == original
    move = json.loads(manifest.read_text(encoding="utf-8"))["moves"][0]
    assert move["disposition"] == "deferred_delete"
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


def _tracked_recipe_repo(
    tmp_path: Path,
) -> tuple[Path, str, Path, bytes, bytes, bytes]:
    repo = tmp_path / "tracked-recipe-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe = repo / "backend-py/scraper_config/unis/portable.yaml"
    recipe.parent.mkdir(parents=True)
    head_content = b"discovery:\n  bfs_page_budget: 3\n"
    target_content = b"discovery:\n  bfs_page_budget: 5\n"
    operator_content = b"discovery:\n  bfs_page_budget: 9\n"
    recipe.write_bytes(head_content)
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "base recipe")
    base = _git(repo, "rev-parse", "HEAD")
    recipe.write_bytes(target_content)
    _git(repo, "add", str(recipe.relative_to(repo)))
    _git(repo, "commit", "-qm", "target recipe")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", base)
    recipe.write_bytes(operator_content)
    return (
        repo,
        target,
        recipe,
        head_content,
        target_content,
        operator_content,
    )


def test_guarded_release_restores_validated_tracked_recipe_after_pull(
    tmp_path: Path,
) -> None:
    repo, target, recipe, head_content, _, operator_content = (
        _tracked_recipe_repo(tmp_path)
    )
    manifest = tmp_path / "tracked-manifest.json"

    preserved = prepare_tracked_recipe_edits(repo, target, manifest)

    assert preserved == [recipe]
    assert recipe.read_bytes() == head_content
    assert _git(repo, "status", "--porcelain") == ""
    _git(repo, "checkout", "-q", target)
    restored = restore_tracked_recipe_edits(manifest)
    assert restored == [recipe]
    assert recipe.read_bytes() == operator_content
    backups = finalize_tracked_recipe_edits(manifest)
    assert len(backups) == 1
    assert not manifest.exists()
    assert not backups[0].exists()


def test_guarded_release_rejects_unknown_tracked_change_before_mutation(
    tmp_path: Path,
) -> None:
    repo, target, recipe, _, _, operator_content = _tracked_recipe_repo(tmp_path)
    unknown = repo / "README.md"
    unknown.write_text("base\n", encoding="utf-8")
    _git(repo, "add", unknown.name)
    _git(repo, "commit", "-qm", "tracked readme")
    target = _git(repo, "rev-parse", "HEAD")
    unknown.write_text("operator edit\n", encoding="utf-8")
    manifest = tmp_path / "tracked-manifest.json"

    with pytest.raises(
        ReleaseCollisionError,
        match="Unknown tracked worktree changes block release",
    ):
        prepare_tracked_recipe_edits(repo, target, manifest)

    assert recipe.read_bytes() == operator_content
    assert unknown.read_text(encoding="utf-8") == "operator edit\n"
    assert not manifest.exists()


def test_guarded_release_detects_recipe_change_during_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, target, recipe, _, _, _ = _tracked_recipe_repo(tmp_path)
    manifest = tmp_path / "tracked-manifest.json"
    raced_content = b"discovery:\n  bfs_page_budget: 11\n"
    original_prepare = generated_configs._prepare_tracked_recipe_entry

    def race_then_prepare(root: Path, entry: dict[str, object]) -> None:
        recipe.write_bytes(raced_content)
        original_prepare(root, entry)

    monkeypatch.setattr(
        generated_configs, "_prepare_tracked_recipe_entry", race_then_prepare
    )

    with pytest.raises(
        ReleaseCollisionError,
        match="changed during release preparation",
    ):
        prepare_tracked_recipe_edits(repo, target, manifest)

    assert recipe.read_bytes() == raced_content
    assert manifest.exists()


def test_guarded_release_restore_fails_closed_on_post_pull_change(
    tmp_path: Path,
) -> None:
    repo, target, recipe, _, _, _ = _tracked_recipe_repo(tmp_path)
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    raced_content = b"discovery:\n  bfs_page_budget: 13\n"
    recipe.write_bytes(raced_content)

    with pytest.raises(
        ReleaseCollisionError, match="changed after release snapshot"
    ):
        restore_tracked_recipe_edits(manifest)

    assert recipe.read_bytes() == raced_content
    assert manifest.exists()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    backup = repo / manifest_data["entries"][0]["backup"]
    assert backup.exists()


def test_guarded_release_rejects_target_recipe_symlink(
    tmp_path: Path,
) -> None:
    repo, target, recipe, _, _, operator_content = _tracked_recipe_repo(tmp_path)
    recipe_relative = str(recipe.relative_to(repo))
    _git(repo, "restore", "--", recipe_relative)
    _git(repo, "checkout", "-q", target)
    recipe.unlink()
    recipe.symlink_to("elsewhere.yaml")
    _git(repo, "add", recipe_relative)
    _git(repo, "commit", "-qm", "replace recipe with symlink")
    symlink_target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "HEAD~2")
    recipe.write_bytes(operator_content)

    with pytest.raises(
        ReleaseCollisionError,
        match="not a regular non-executable Git file",
    ):
        prepare_tracked_recipe_edits(
            repo, symlink_target, tmp_path / "tracked-manifest.json"
        )

    assert recipe.read_bytes() == operator_content


def test_guarded_release_detects_leaf_recreation_during_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, target, recipe, _, _, _ = _tracked_recipe_repo(tmp_path)
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    raced_content = b"discovery:\n  bfs_page_budget: 17\n"
    original_write = generated_configs._write_relative_exclusively
    recipe_relative = str(recipe.relative_to(repo))

    def recreate_then_write(
        root: Path, relative: str, content: bytes, mode: int
    ) -> None:
        if root == repo and relative == recipe_relative:
            recipe.write_bytes(raced_content)
        original_write(root, relative, content, mode)

    monkeypatch.setattr(
        generated_configs, "_write_relative_exclusively", recreate_then_write
    )

    with pytest.raises(
        ReleaseCollisionError, match="recreated concurrently"
    ):
        restore_tracked_recipe_edits(manifest)

    assert recipe.read_bytes() == raced_content
    assert manifest.exists()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert (repo / manifest_data["entries"][0]["backup"]).exists()
    assert (repo / manifest_data["entries"][0]["quarantine"]).exists()


def test_guarded_release_prevalidates_all_recipes_before_restoring_any(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "multi-recipe-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe_root = repo / "backend-py/scraper_config/unis"
    recipe_root.mkdir(parents=True)
    first = recipe_root / "first.yaml"
    second = recipe_root / "second.yaml"
    first.write_text("value: base-first\n", encoding="utf-8")
    second.write_text("value: base-second\n", encoding="utf-8")
    _git(repo, "add", str(recipe_root.relative_to(repo)))
    _git(repo, "commit", "-qm", "base recipes")
    base = _git(repo, "rev-parse", "HEAD")
    first.write_text("value: target-first\n", encoding="utf-8")
    second.write_text("value: target-second\n", encoding="utf-8")
    _git(repo, "add", str(recipe_root.relative_to(repo)))
    _git(repo, "commit", "-qm", "target recipes")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", base)
    first.write_text("value: operator-first\n", encoding="utf-8")
    second.write_text("value: operator-second\n", encoding="utf-8")
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    second.write_text("value: raced-second\n", encoding="utf-8")

    with pytest.raises(
        ReleaseCollisionError, match="changed after release snapshot"
    ):
        restore_tracked_recipe_edits(manifest)

    assert first.read_text(encoding="utf-8") == "value: target-first\n"
    assert second.read_text(encoding="utf-8") == "value: raced-second\n"
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert all(
        (repo / entry["backup"]).exists()
        for entry in manifest_data["entries"]
    )


def test_guarded_release_post_checkout_fence_rejects_late_unknown_edit(
    tmp_path: Path,
) -> None:
    repo, _, recipe, _, _, operator_content = _tracked_recipe_repo(tmp_path)
    unknown = repo / "README.md"
    unknown.write_text("base\n", encoding="utf-8")
    _git(repo, "add", unknown.name)
    _git(repo, "commit", "-qm", "tracked readme")
    target = _git(repo, "rev-parse", "HEAD")
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    restore_tracked_recipe_edits(manifest)
    assert recipe.read_bytes() == operator_content
    unknown.write_text("late edit\n", encoding="utf-8")

    with pytest.raises(
        ReleaseCollisionError,
        match="Unknown tracked worktree changes appeared during release",
    ):
        verify_tracked_recipe_edits(manifest)


def test_tracked_recipe_finalization_commits_before_partial_backup_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "finalize-cleanup-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    recipe_root = repo / "backend-py/scraper_config/unis"
    recipe_root.mkdir(parents=True)
    for name in ("first.yaml", "second.yaml"):
        (recipe_root / name).write_text("value: base\n", encoding="utf-8")
    _git(repo, "add", str(recipe_root.relative_to(repo)))
    _git(repo, "commit", "-qm", "base recipes")
    base = _git(repo, "rev-parse", "HEAD")
    for name in ("first.yaml", "second.yaml"):
        (recipe_root / name).write_text("value: target\n", encoding="utf-8")
    _git(repo, "add", str(recipe_root.relative_to(repo)))
    _git(repo, "commit", "-qm", "target recipes")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", base)
    for name in ("first.yaml", "second.yaml"):
        (recipe_root / name).write_text("value: operator\n", encoding="utf-8")
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    restore_tracked_recipe_edits(manifest)
    original_unlink = generated_configs._unlink_relative
    calls = 0

    def fail_second_backup(root: Path, relative: str) -> None:
        nonlocal calls
        if "release-tracked-config-backups" in relative:
            calls += 1
            if calls == 2:
                raise PermissionError("injected backup deletion failure")
        original_unlink(root, relative)

    monkeypatch.setattr(
        generated_configs, "_unlink_relative", fail_second_backup
    )

    removed = finalize_tracked_recipe_edits(manifest)

    assert len(removed) == 1
    assert not manifest.exists()
    assert manifest.with_name(f"{manifest.name}.committed").exists()
    manifest_data = json.loads(
        manifest.with_name(f"{manifest.name}.committed").read_text(
            encoding="utf-8"
        )
    )
    remaining = [
        repo / entry["backup"]
        for entry in manifest_data["entries"]
        if (repo / entry["backup"]).exists()
    ]
    assert len(remaining) == 1


def test_tracked_recipe_finalization_revalidates_sources_at_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo, target, recipe, _, _, _ = _tracked_recipe_repo(tmp_path)
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    restore_tracked_recipe_edits(manifest)
    original_validate = generated_configs._validate_tracked_recipe_finalization
    calls = 0

    def change_before_commit(
        root: Path, entries: list[dict[str, object]]
    ) -> list[Path]:
        nonlocal calls
        calls += 1
        if calls == 2:
            recipe.write_text("value: raced-at-commit\n", encoding="utf-8")
        return original_validate(root, entries)

    monkeypatch.setattr(
        generated_configs,
        "_validate_tracked_recipe_finalization",
        change_before_commit,
    )

    with pytest.raises(
        ReleaseCollisionError, match="operator recipe is not restored"
    ):
        finalize_tracked_recipe_edits(manifest)

    assert manifest.exists()
    assert not manifest.with_name(f"{manifest.name}.committed").exists()
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert (repo / manifest_data["entries"][0]["backup"]).exists()


def test_aborted_release_restores_and_cleans_tracked_recipe_backup(
    tmp_path: Path,
) -> None:
    repo, target, recipe, head_content, _, operator_content = (
        _tracked_recipe_repo(tmp_path)
    )
    manifest = tmp_path / "tracked-manifest.json"
    prepare_tracked_recipe_edits(repo, target, manifest)
    assert recipe.read_bytes() == head_content

    assert restore_tracked_recipe_edits(manifest) == [recipe]
    backups = finalize_tracked_recipe_edits(manifest)

    assert recipe.read_bytes() == operator_content
    assert not manifest.exists()
    assert all(not backup.exists() for backup in backups)


def test_guarded_release_restores_recipes_before_restart_and_stops_on_cleanup_failure() -> None:
    script = (DEPLOY_DIR / "guarded_release.sh").read_text(encoding="utf-8")
    checkout = script.index('"$revision_fence" \\\n  checkout')
    restore = script.index("restore-tracked", checkout)
    restart = script.index(
        "systemctl restart uni-api-py.service uni-celery.service"
    )
    finalize = script.index("finalize-tracked", restart)

    assert checkout < restore < restart < finalize
    failure = script[
        script.index('if [ "$rollback_failed" = 1 ]') : script.index(
            "\n  fi", script.index("systemctl stop uni-celery.service")
        )
    ]
    assert 'cancel_consumer("scrape", reply=True' in failure
    assert ".active_queues()" in failure
    assert "systemctl stop uni-celery.service" in failure
    assert failure.index("cancel_consumer") < failure.index("systemctl stop")


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


def test_production_release_reports_redundant_overlays_after_checkout() -> None:
    script = (DEPLOY_DIR / "guarded_release.sh").read_text(
        encoding="utf-8"
    )
    helper = (
        DEPLOY_DIR / "post_checkout_overlay_audit.py"
    ).read_text(encoding="utf-8")

    checkout_verified = script.index('"$revision_fence" \\\n  checkout')
    audit = script.index("post_checkout_overlay_audit.py")
    restart = script.index(
        "systemctl restart uni-api-py.service uni-celery.service"
    )

    assert checkout_verified < audit < restart
    assert "REDUNDANT_CONFIG_OVERLAY_COUNT=" in helper
    assert "REDUNDANT_CONFIG_OVERLAY_PATH=" in helper
    assert "cleanup-overlays" not in script


def test_production_release_blocks_only_corrupt_repository_after_audit_failure() -> None:
    script = (DEPLOY_DIR / "guarded_release.sh").read_text(
        encoding="utf-8"
    )
    audit_failure = script.split("post_checkout_overlay_audit.py", maxsplit=1)[1]

    assert "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking" in audit_failure
    assert "Repository corruption detected" in audit_failure
    assert 'if [ "$overlay_audit_status" = 42 ]' in audit_failure
    assert audit_failure.index('= 42 ]') < audit_failure.index("exit 1")
    gate = audit_failure[
        audit_failure.index('if [ "$overlay_audit_status" = 42 ]') :
        audit_failure.index("\nfi", audit_failure.index("exit 1"))
    ]
    corrupt_branch, inconclusive_branch = gate.split("elif", maxsplit=1)
    assert "exit 1" in corrupt_branch
    assert "exit " not in inconclusive_branch
    assert audit_failure.index("exit 1") < audit_failure.index(
        "systemctl restart uni-api-py.service uni-celery.service"
    )


def test_post_checkout_audit_reports_count_paths_and_changes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    original = overlay.read_bytes()
    evidence_path = tmp_path / "overlay-audit.json"

    assert post_checkout_audit.run_post_checkout_audit(
        repo,
        evidence_path=evidence_path,
    ) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        "REDUNDANT_CONFIG_OVERLAY_COUNT=1\n"
        "REDUNDANT_CONFIG_OVERLAY_PATH="
        "backend-py/scraper_config/runtime_unis/portable_11.yaml\n"
    )
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == {
        "status": "ok",
        "redundant_overlay_count": 1,
        "redundant_overlay_paths": [
            "backend-py/scraper_config/runtime_unis/portable_11.yaml"
        ],
    }
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert overlay.read_bytes() == original


def test_post_checkout_audit_failure_is_non_blocking_when_fsck_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        post_checkout_audit,
        "find_redundant_generated_overlays",
        lambda _root: (_ for _ in ()).throw(OSError("audit unavailable")),
    )
    monkeypatch.setattr(
        post_checkout_audit,
        "repository_corruption_confirmed",
        lambda _root: False,
    )

    evidence_path = tmp_path / "overlay-audit.json"
    assert post_checkout_audit.run_post_checkout_audit(
        tmp_path,
        evidence_path=evidence_path,
    ) == 0
    assert capsys.readouterr().err == (
        "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking\n"
    )
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == {
        "status": "warning"
    }


def test_post_checkout_audit_blocks_only_confirmed_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        post_checkout_audit,
        "find_redundant_generated_overlays",
        lambda _root: (_ for _ in ()).throw(OSError("audit unavailable")),
    )
    monkeypatch.setattr(
        post_checkout_audit,
        "repository_corruption_confirmed",
        lambda _root: True,
    )

    assert (
        post_checkout_audit.run_post_checkout_audit(tmp_path)
        == post_checkout_audit.CORRUPTION_EXIT
    )


def test_post_checkout_audit_detects_a_real_missing_git_object(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "damaged-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    tracked = repo / "tracked.txt"
    tracked.write_text("reachable blob\n", encoding="utf-8")
    _git(repo, "add", tracked.name)
    _git(repo, "commit", "-qm", "reachable object")
    blob = _git(repo, "rev-parse", "HEAD:tracked.txt").strip()
    object_path = repo / ".git/objects" / blob[:2] / blob[2:]
    assert object_path.is_file()
    object_path.unlink()

    assert post_checkout_audit.repository_corruption_confirmed(repo) is True
    assert (
        post_checkout_audit.run_post_checkout_audit(repo)
        == post_checkout_audit.CORRUPTION_EXIT
    )


def test_real_corruption_status_stops_exact_release_gate_before_restart(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "damaged-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    tracked = repo / "tracked.txt"
    tracked.write_text("reachable blob\n", encoding="utf-8")
    _git(repo, "add", tracked.name)
    _git(repo, "commit", "-qm", "reachable object")
    blob = _git(repo, "rev-parse", "HEAD:tracked.txt").strip()
    (repo / ".git/objects" / blob[:2] / blob[2:]).unlink()

    helper_result = subprocess.run(
        [
            sys.executable,
            str(DEPLOY_DIR / "post_checkout_overlay_audit.py"),
            "--repo-root",
            str(repo),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert helper_result.returncode == post_checkout_audit.CORRUPTION_EXIT

    release_script = (
        DEPLOY_DIR / "guarded_release.sh"
    ).read_text(encoding="utf-8")
    gate_start = release_script.index('if [ "$overlay_audit_status" = 42 ]')
    gate_end = release_script.index("\nfi", gate_start) + len("\nfi")
    exact_gate = release_script[gate_start:gate_end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    restart_marker = tmp_path / "restart-reached"
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        f"#!/bin/sh\nprintf reached > {restart_marker}\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    gate_result = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"overlay_audit_status={helper_result.returncode}\n"
                f"{exact_gate}\n"
                "systemctl restart uni-api-py.service uni-celery.service\n"
            ),
        ],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert gate_result.returncode == 1
    assert not restart_marker.exists()
    for non_corruption_status in (0, 7):
        allowed_result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f"overlay_audit_status={non_corruption_status}\n"
                    f"{exact_gate}\n"
                    "systemctl restart uni-api-py.service uni-celery.service\n"
                ),
            ],
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert allowed_result.returncode == 0
        assert restart_marker.read_text(encoding="utf-8") == "reached"
        restart_marker.unlink()


def test_post_checkout_audit_rejects_control_character_paths_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        post_checkout_audit,
        "find_redundant_generated_overlays",
        lambda _root: [{"overlay": "backend-py/scraper_config/runtime_unis/a\nb.yaml"}],
    )
    monkeypatch.setattr(
        post_checkout_audit,
        "repository_corruption_confirmed",
        lambda _root: False,
    )

    assert post_checkout_audit.run_post_checkout_audit(tmp_path) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING=failed_non_blocking\n"
    )


@pytest.mark.parametrize(
    "message",
    [
        "missing blob abcdef",
        "broken link from tree abcdef",
        "refs/heads/main: invalid sha1 pointer abcdef",
        "fatal: bad object abcdef",
        "object corrupt or missing: abcdef",
        "hash mismatch for object abcdef",
    ],
)
def test_repository_corruption_classifier_accepts_only_known_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: str,
) -> None:
    monkeypatch.setattr(
        post_checkout_audit.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=message,
        ),
    )

    assert post_checkout_audit.repository_corruption_confirmed(tmp_path) is True


@pytest.mark.parametrize(
    "result_or_error",
    [
        SimpleNamespace(returncode=0, stdout="", stderr="missing blob abcdef"),
        SimpleNamespace(returncode=1, stdout="", stderr="permission denied"),
        SimpleNamespace(returncode=128, stdout="", stderr="not a git repository"),
        FileNotFoundError("git unavailable"),
        subprocess.TimeoutExpired(["git", "fsck"], 60),
    ],
)
def test_repository_corruption_classifier_fails_open_when_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result_or_error: object,
) -> None:
    def run(*_args: object, **_kwargs: object) -> object:
        if isinstance(result_or_error, BaseException):
            raise result_or_error
        return result_or_error

    monkeypatch.setattr(post_checkout_audit.subprocess, "run", run)

    assert post_checkout_audit.repository_corruption_confirmed(tmp_path) is False


@pytest.mark.parametrize(
    ("corruption_confirmed", "expected_status"),
    [(False, 0), (True, post_checkout_audit.CORRUPTION_EXIT)],
)
def test_post_checkout_report_failure_uses_corruption_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corruption_confirmed: bool,
    expected_status: int,
) -> None:
    emitted: list[tuple[object, str]] = []
    monkeypatch.setattr(
        post_checkout_audit,
        "find_redundant_generated_overlays",
        lambda _root: [],
    )
    monkeypatch.setattr(
        post_checkout_audit,
        "repository_corruption_confirmed",
        lambda _root: corruption_confirmed,
    )

    def emit(stream: object, message: str) -> bool:
        emitted.append((stream, message))
        return stream is not post_checkout_audit.sys.stdout

    monkeypatch.setattr(post_checkout_audit, "_safe_emit", emit)

    assert post_checkout_audit.run_post_checkout_audit(tmp_path) == expected_status
    if corruption_confirmed:
        assert emitted == [
            (
                post_checkout_audit.sys.stderr,
                "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING="
                "repository_corruption_confirmed",
            )
        ]
    else:
        assert emitted == [
            (
                post_checkout_audit.sys.stdout,
                "REDUNDANT_CONFIG_OVERLAY_COUNT=0",
            ),
            (
                post_checkout_audit.sys.stderr,
                "REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING="
                "report_failed_non_blocking",
            ),
        ]


@pytest.mark.parametrize(
    ("corruption_confirmed", "expected_status"),
    [(False, 0), (True, post_checkout_audit.CORRUPTION_EXIT)],
)
def test_closed_stdout_preserves_post_checkout_audit_exit_status(
    corruption_confirmed: bool,
    expected_status: int,
) -> None:
    script = (
        "from pathlib import Path\n"
        "import deploy.post_checkout_overlay_audit as audit\n"
        "audit.find_redundant_generated_overlays = lambda _root: []\n"
        "audit.repository_corruption_confirmed = "
        f"lambda _root: {corruption_confirmed!r}\n"
        "raise SystemExit(audit.run_post_checkout_audit(Path('.')))\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(BACKEND_ROOT)
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", script],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    process.stdout.close()
    assert process.stderr is not None
    stderr = process.stderr.read()

    assert process.wait(timeout=10) == expected_status
    expected_warning = (
        "repository_corruption_confirmed"
        if corruption_confirmed
        else "report_failed_non_blocking"
    )
    assert f"REDUNDANT_CONFIG_OVERLAY_AUDIT_WARNING={expected_warning}" in stderr
    assert "Exception ignored" not in stderr


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


def test_successful_identity_persists_sanitized_overlay_audit_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release = "d" * 40
    deployment_evidence = tmp_path / "deployments.jsonl"
    audit_evidence = tmp_path / "overlay-audit.json"
    audit_evidence.write_text(
        json.dumps(
            {
                "status": "ok",
                "redundant_overlay_count": 1,
                "redundant_overlay_paths": [
                    "backend-py/scraper_config/runtime_unis/portable_11.yaml"
                ],
                "overlay_sha256": "must-not-persist",
                "absolute_path": "/opt/university-portal/private",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 0.25, "uni-celery": 0.5},
        ),
    )

    asyncio.run(
        _main(
            Namespace(
                release_identity_only=True,
                journal_since="2026-09-17T00:00:00+00:00",
                release_identity_timeout_seconds=15,
                deployment_evidence_path=deployment_evidence,
                overlay_audit_evidence_path=audit_evidence,
                recent_deployment_evidence=None,
            )
        )
    )

    record = json.loads(deployment_evidence.read_text(encoding="utf-8"))
    assert record["overlay_audit_status"] == "ok"
    assert record["redundant_overlay_count"] == 1
    assert record["redundant_overlay_paths"] == [
        "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    ]
    assert "overlay_sha256" not in record
    assert "absolute_path" not in record


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps({"status": "ok", "redundant_overlay_count": 1,
                    "redundant_overlay_paths": ["/opt/private.yaml"]}),
        json.dumps({"status": "warning", "error": "secret details"}),
    ],
)
def test_untrusted_overlay_audit_handoff_becomes_sanitized_warning(
    tmp_path: Path,
    payload: str,
) -> None:
    evidence_path = tmp_path / "overlay-audit.json"
    evidence_path.write_text(payload, encoding="utf-8")

    assert read_overlay_audit_evidence(evidence_path) == {
        "overlay_audit_status": "warning"
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe\xfa",
        b" " * (MAX_OVERLAY_AUDIT_EVIDENCE_BYTES + 1),
        (
            b'{"status":"ok","redundant_overlay_count":'
            + b"9" * 5_000
            + b',"redundant_overlay_paths":[]}'
        ),
        b"[" * 10_000 + b"]" * 10_000,
    ],
)
def test_invalid_or_oversized_overlay_handoff_cannot_block_identity_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
) -> None:
    release = "e" * 40
    audit_evidence = tmp_path / "overlay-audit.json"
    deployment_evidence = tmp_path / "deployments.jsonl"
    audit_evidence.write_bytes(payload)
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 0.1, "uni-celery": 0.2},
        ),
    )

    asyncio.run(
        _main(
            Namespace(
                release_identity_only=True,
                journal_since="2026-09-17T00:00:00+00:00",
                release_identity_timeout_seconds=15,
                deployment_evidence_path=deployment_evidence,
                overlay_audit_evidence_path=audit_evidence,
                recent_deployment_evidence=None,
            )
        )
    )

    record = json.loads(deployment_evidence.read_text(encoding="utf-8"))
    assert record["overlay_audit_status"] == "warning"
    assert "redundant_overlay_count" not in record
    assert "redundant_overlay_paths" not in record


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

def test_finalize_prevalidates_every_deferred_backup_before_deleting(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery:
  bfs_page_budget: 3
"""
    repo, target, _collision = _collision_repo(tmp_path, stub_body=body)
    manifest = tmp_path / "manifest.json"
    preserved = reconcile_generated_config_collisions(repo, target, manifest)
    _git(repo, "checkout", "-q", target)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    missing_move = dict(payload["moves"][0])
    missing_move["destination"] = (
        "backend-py/.git/release-generated-config-backups/missing.yaml"
    )
    payload["moves"].append(missing_move)
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ReleaseCollisionError, match="Cannot safely finalize generated config"
    ):
        finalize_generated_config_collisions(manifest)

    assert preserved[0].exists()
    assert manifest.exists()

def test_failed_release_after_checkout_keeps_generated_only_overlay(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery:
  max_candidates: 77
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    original = collision.read_bytes()
    manifest = tmp_path / "manifest.json"
    reconcile_generated_config_collisions(repo, target, manifest)
    _git(repo, "checkout", "-q", target)

    restored = rollback_generated_config_collisions(manifest)

    overlay = repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    assert restored == [overlay]
    assert overlay.read_bytes() == original
    assert not manifest.exists()

def test_failed_release_after_checkout_restores_deferred_collision_as_overlay(
    tmp_path: Path,
) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery:
  bfs_page_budget: 3
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    original = collision.read_bytes()
    manifest = tmp_path / "manifest.json"
    reconcile_generated_config_collisions(repo, target, manifest)
    _git(repo, "checkout", "-q", target)

    restored = rollback_generated_config_collisions(manifest)

    overlay = repo / "backend-py/scraper_config/runtime_unis/portable_11.yaml"
    assert restored == [overlay]
    assert overlay.read_bytes() == original
    assert collision.read_text(encoding="utf-8") == (
        "discovery:\n  bfs_page_budget: 9\n"
    )
    assert not manifest.exists()

def test_successful_release_finalizes_deferred_collision(tmp_path: Path) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery:
  bfs_page_budget: 3
"""
    repo, target, collision = _collision_repo(tmp_path, stub_body=body)
    manifest = tmp_path / "manifest.json"
    preserved = reconcile_generated_config_collisions(repo, target, manifest)
    _git(repo, "checkout", "-q", target)

    removed = finalize_generated_config_collisions(manifest)

    assert removed == preserved
    assert not preserved[0].exists()
    assert not manifest.exists()
    assert collision.exists()
