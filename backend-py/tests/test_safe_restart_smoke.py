"""Focused contracts for the production-safe restart smoke command."""
from __future__ import annotations

from pathlib import Path

import pytest

from deploy.safe_restart_smoke import (
    DEFAULT_COURSE_URL,
    DEFAULT_EXPECTED_SKIP_REASON,
    DEFAULT_UNIVERSITY_ID,
    SmokeFailure,
    resolve_expected_release,
    validate_done_payload,
    validate_idle_counts,
)
from app.services.scraper.orchestrator import _is_safe_restart_smoke_payload


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval"])
def test_aborts_when_any_active_job_status_is_present(status: str) -> None:
    counts = {"queued": 0, "running": 0, "awaiting_approval": 0}
    counts[status] = 1

    with pytest.raises(SmokeFailure, match=status):
        validate_idle_counts(counts)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {
            "totalFound": 0,
            "imported": 0,
            "skipped": 0,
            "skip_reasons": {},
        },
        {
            "totalFound": 1,
            "imported": 1,
            "skipped": 0,
            "skip_reasons": {},
        },
    ],
)
def test_zero_work_or_non_skip_sample_fails(payload: dict | None) -> None:
    with pytest.raises(SmokeFailure):
        validate_done_payload(payload)


def test_accepts_exact_canonical_default_policy_skip_payload() -> None:
    assert DEFAULT_EXPECTED_SKIP_REASON == "domestic_only"
    validate_done_payload(
        {
            "totalFound": 1,
            "imported": 0,
            "skipped": 1,
            "skip_reasons": {"domestic_only": 1},
        }
    )


def test_rejects_staged_rows_errors_or_extra_skip_reasons() -> None:
    clean = {
        "totalFound": 1,
        "imported": 0,
        "skipped": 1,
        "errors": 0,
        "skip_reasons": {"domestic_only": 1},
    }
    with pytest.raises(SmokeFailure, match="staged_rows=1"):
        validate_done_payload(clean, staged_rows=1)
    with pytest.raises(SmokeFailure, match="errors=1"):
        validate_done_payload({**clean, "errors": 1})
    with pytest.raises(SmokeFailure, match="canonical"):
        validate_done_payload(
            {**clean, "skip_reasons": {"domestic_only": 1, "parser_error": 1}}
        )


def test_checked_in_sample_resolves_university_by_hostname() -> None:
    assert DEFAULT_UNIVERSITY_ID is None
    assert DEFAULT_COURSE_URL == (
        "https://www.torrens.edu.au/courses/business/"
        "bachelor-of-applied-business-marketing-partnership-with-ducere"
    )


def test_managed_database_environment_loads_before_app_imports() -> None:
    source = Path("deploy/safe_restart_smoke.py").read_text()
    assert source.index("_load_managed_database_environment()") < source.index(
        "from app.database import AsyncSessionLocal"
    )


def test_smoke_marker_requires_exactly_one_valid_target_url() -> None:
    assert _is_safe_restart_smoke_payload(
        {"safeRestartSmoke": True, "courseUrls": [DEFAULT_COURSE_URL]}
    )
    assert not _is_safe_restart_smoke_payload(
        {
            "safeRestartSmoke": True,
            "courseUrls": [DEFAULT_COURSE_URL, "https://example.edu/course/two"],
        }
    )
    assert not _is_safe_restart_smoke_payload(
        {"safeRestartSmoke": True, "courseUrls": []}
    )


def test_release_file_preserves_full_sha_used_by_running_services(tmp_path) -> None:
    full_sha = "a" * 40
    release_file = tmp_path / ".release.env"
    release_file.write_text(f"RELEASE_REVISION={full_sha}\n", encoding="utf-8")

    assert resolve_expected_release(release_file, fallback="a" * 12) == full_sha


def test_metadata_only_package_uses_release_file_without_git(tmp_path) -> None:
    release_file = tmp_path / ".release.env"
    release_file.write_text("RELEASE_REVISION=build-2026-09-07\n", encoding="utf-8")

    assert (
        resolve_expected_release(release_file, fallback="unknown")
        == "build-2026-09-07"
    )


def test_rejects_legacy_online_only_reason_key() -> None:
    with pytest.raises(SmokeFailure, match="canonical"):
        validate_done_payload(
            {
                "totalFound": 1,
                "imported": 0,
                "skipped": 1,
                "skip_reasons": {"rejected:_online_only": 1},
            }
        )