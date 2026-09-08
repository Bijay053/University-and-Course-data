#!/usr/bin/env python3
"""Read-only-first production restart smoke check.

Run on the deployed host from backend-py. No database write occurs until all
idle, release, service, API, Celery, university, and sample-HTML checks pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MANAGED_DATABASE_ENV_FILE = Path("/etc/university-portal/database.env")
_MANAGED_DATABASE_KEYS = {
    "DATABASE_URL",
    "DATABASE_REQUIRE_TLS",
    "DATABASE_SECRET_VERSION",
    "SSL_CERT_FILE",
}


def _load_managed_database_environment() -> None:
    if not MANAGED_DATABASE_ENV_FILE.is_file():
        return
    with MANAGED_DATABASE_ENV_FILE.open(encoding="utf-8") as stream:
        for raw_line in stream:
            parsed = shlex.split(raw_line, comments=False, posix=True)
            if not parsed:
                continue
            key, value = parsed[0].split("=", 1)
            if key in _MANAGED_DATABASE_KEYS:
                os.environ[key] = value


_load_managed_database_environment()

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import ScrapedCourse, University
from app.models.scrape_runtime import ScrapeRuntimeJob, ScrapeRuntimeLog
from app.release_info import UNKNOWN_RELEASE, get_release_revision

ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")
REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_ENV_FILE = REPO_ROOT / "backend-py" / ".release.env"
DEFAULT_UNIVERSITY_ID = None
DEFAULT_COURSE_URL = (
    "https://www.torrens.edu.au/courses/business/"
    "bachelor-of-applied-business-marketing-partnership-with-ducere"
)
DEFAULT_EXPECTED_SKIP_REASON = "domestic_only"


class SmokeFailure(RuntimeError):
    """A safe-restart precondition or proof failed."""


def validate_idle_counts(counts: Mapping[str, int]) -> None:
    active = {status: int(counts.get(status, 0)) for status in ACTIVE_STATUSES}
    if any(active.values()):
        detail = ", ".join(f"{key}={value}" for key, value in active.items())
        raise SmokeFailure(f"active scrape jobs prevent restart smoke: {detail}")


def validate_done_payload(
    payload: Mapping[str, Any] | None,
    *,
    staged_rows: int = 0,
    expected_skip_reason: str = DEFAULT_EXPECTED_SKIP_REASON,
) -> None:
    if not payload:
        raise SmokeFailure("sample produced no DONE payload")
    total = int(payload.get("totalFound") or 0)
    skipped = int(payload.get("skipped") or 0)
    imported = int(payload.get("imported") or 0)
    errors = int(payload.get("errors") or 0)
    reasons = payload.get("skip_reasons")
    if total != 1 or skipped != 1 or imported != 0 or errors != 0 or staged_rows != 0:
        raise SmokeFailure(
            "sample did not perform exactly one policy-skip unit of work "
            f"(totalFound={total}, skipped={skipped}, imported={imported}, "
            f"errors={errors}, staged_rows={staged_rows})"
        )
    expected_reasons = {expected_skip_reason: 1}
    if not isinstance(reasons, Mapping) or dict(reasons) != expected_reasons:
        raise SmokeFailure(
            f"DONE payload lacks canonical {expected_skip_reason}=1"
        )


def _run(command: list[str], *, timeout: float = 20) -> str:
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=timeout
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode:
        raise SmokeFailure(f"{' '.join(command)} failed: {output[:500]}")
    return output


def resolve_expected_release(
    release_file: Path = RELEASE_ENV_FILE, fallback: str | None = None
) -> str:
    """Read the systemd release file, falling back to process/Git metadata."""
    try:
        for line in release_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "RELEASE_REVISION" and value.strip():
                return value.strip()
    except OSError:
        pass
    return fallback if fallback is not None else get_release_revision()


def _verify_release_and_services() -> str:
    release = resolve_expected_release()
    if release == UNKNOWN_RELEASE:
        raise SmokeFailure("deployed release revision is unavailable")
    try:
        head = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]).strip()
    except SmokeFailure:
        head = ""
    if head and not (
        head.lower().startswith(release.lower())
        or release.lower().startswith(head.lower())
    ):
        raise SmokeFailure(f"release revision {release!r} does not match Git HEAD {head}")
    for unit in ("uni-api-py", "uni-celery"):
        _run(["systemctl", "is-active", "--quiet", unit])
        pid = _run(
            ["systemctl", "show", unit, "--property=MainPID", "--value"]
        ).strip()
        if not pid.isdigit() or int(pid) <= 0:
            raise SmokeFailure(f"{unit} has no live MainPID")
        try:
            process_env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError as exc:
            raise SmokeFailure(f"cannot inspect {unit} process environment") from exc
        expected = f"RELEASE_REVISION={release}".encode()
        if expected not in process_env:
            raise SmokeFailure(
                f"{unit} running process does not have RELEASE_REVISION={release}"
            )
    return release


def _fetch_json(url: str, timeout: float = 10) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        if response.status != 200:
            raise SmokeFailure(f"{url} returned HTTP {response.status}")
        return json.loads(response.read(64_000))


def _verify_api_and_celery(api_health_url: str) -> None:
    health = _fetch_json(api_health_url)
    if health.get("status") != "ok":
        raise SmokeFailure(f"API health is not ok: {health}")
    ping = _run(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app",
            "inspect",
            "ping",
            "--timeout",
            "10",
        ],
        timeout=20,
    )
    if "pong" not in ping.lower():
        raise SmokeFailure("Celery inspect ping returned no pong")


def _verify_ordinary_html(url: str) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "UniversityPortal-SafeRestartSmoke/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        content_type = response.headers.get_content_type()
        body = response.read(512)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise SmokeFailure(f"sample is not ordinary HTML (content-type={content_type})")
    if b"<html" not in body.lower() and b"<!doctype html" not in body.lower():
        raise SmokeFailure("sample response does not begin with an HTML document")


async def _active_counts() -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ScrapeRuntimeJob.status, func.count())
                .where(ScrapeRuntimeJob.status.in_(ACTIVE_STATUSES))
                .group_by(ScrapeRuntimeJob.status)
            )
        ).all()
    return {str(status): int(count) for status, count in rows}


async def _create_and_dispatch(university_id: int, course_url: str) -> str:
    from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock

    job_id = f"safe_restart_smoke_{uuid.uuid4().hex}"
    async with AsyncSessionLocal() as db:
        university = await db.get(University, university_id)
        if university is None:
            raise SmokeFailure(f"university_id={university_id} does not exist")
        job = ScrapeRuntimeJob(
            runtime_job_id=job_id,
            university_id=university.id,
            university_name=university.name,
            url=university.scrape_url or university.website or course_url,
            job_type="safe_restart_smoke",
            status="queued",
            fast_mode=True,
            request_payload={
                "safeRestartSmoke": True,
                "courseUrls": [course_url],
            },
        )
        db.add(job)
        await db.commit()
    try:
        scrape_university.delay(job_id)
        set_initial_dispatch_lock(job_id)
    except Exception:
        async with AsyncSessionLocal() as db:
            job = await db.get(ScrapeRuntimeJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_message = "safe restart smoke dispatch failed"
                await db.commit()
        raise
    return job_id


async def _resolve_university_id(
    explicit_university_id: int | None, course_url: str
) -> int:
    if explicit_university_id is not None:
        return explicit_university_id
    course_host = (urlparse(course_url).hostname or "").lower().removeprefix("www.")
    if not course_host:
        raise SmokeFailure("sample course URL has no hostname")
    async with AsyncSessionLocal() as db:
        universities = (await db.execute(select(University))).scalars().all()
    matches = []
    for university in universities:
        hosts = {
            (urlparse(value).hostname or "").lower().removeprefix("www.")
            for value in (university.scrape_url, university.website)
            if value
        }
        if any(
            course_host == host
            or course_host.endswith("." + host)
            or host.endswith("." + course_host)
            for host in hosts
            if host
        ):
            matches.append(university.id)
    if len(matches) != 1:
        raise SmokeFailure(
            f"sample course hostname matched {len(matches)} universities; "
            "pass --university-id explicitly"
        )
    return matches[0]


async def _wait_for_done(
    job_id: str, timeout_seconds: int
) -> tuple[Mapping[str, Any], int]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionLocal() as db:
            job = await db.get(ScrapeRuntimeJob, job_id)
            if job is None:
                raise SmokeFailure("smoke job disappeared")
            if job.status not in ACTIVE_STATUSES:
                if job.status != "completed":
                    raise SmokeFailure(
                        f"smoke job ended as {job.status}: "
                        f"{job.error_message or 'no error'}"
                    )
                row = (
                    await db.execute(
                        select(ScrapeRuntimeLog.payload)
                        .where(
                            ScrapeRuntimeLog.runtime_job_id == job_id,
                            ScrapeRuntimeLog.event == "done",
                        )
                        .order_by(ScrapeRuntimeLog.sequence.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if isinstance(row, Mapping):
                    staged_rows = int(
                        (
                            await db.execute(
                                select(func.count())
                                .select_from(ScrapedCourse)
                                .where(ScrapedCourse.scrape_job_id == job_id)
                            )
                        ).scalar_one()
                    )
                    return row, staged_rows
                raise SmokeFailure("completed sample has no persisted DONE payload")
        await asyncio.sleep(2)
    raise SmokeFailure(f"sample did not finish within {timeout_seconds}s")


async def _main(args: argparse.Namespace) -> None:
    counts = await _active_counts()
    validate_idle_counts(counts)
    release = _verify_release_and_services()
    _verify_api_and_celery(args.api_health_url)
    _verify_ordinary_html(args.course_url)

    # Re-check immediately before the first write to close the long preflight gap.
    validate_idle_counts(await _active_counts())
    university_id = await _resolve_university_id(args.university_id, args.course_url)
    job_id = await _create_and_dispatch(university_id, args.course_url)
    payload, staged_rows = await _wait_for_done(job_id, args.timeout_seconds)
    validate_done_payload(payload, staged_rows=staged_rows)
    print(f"safe restart smoke passed: release={release} job_id={job_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--university-id", type=int, default=DEFAULT_UNIVERSITY_ID)
    parser.add_argument("--course-url", default=DEFAULT_COURSE_URL)
    parser.add_argument(
        "--api-health-url", default="http://127.0.0.1:8000/api/health"
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except (SmokeFailure, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"safe restart smoke aborted: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())