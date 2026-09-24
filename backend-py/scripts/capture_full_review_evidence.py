#!/usr/bin/env python3
"""Capture a privacy-safe, read-only review evidence snapshot.

This is deliberately an observation tool, not a scraper or a repair tool.  It
does not fetch university pages and it never writes a database row or a config
stub.  The JSON is suitable for saving before and after a review:

    python scripts/capture_full_review_evidence.py --university-id 89 > before.json
    python scripts/capture_full_review_evidence.py --university-id 89 \
      --job-id <runtime-job-id> --baseline before.json > after.json

Counts describe rows observed at one point in time; they do *not* certify that
the university catalogue is complete unless the scrape job itself completed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# Make ``python scripts/...`` work from backend-py without requiring installation.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models.academic_requirement import AcademicRequirement  # noqa: E402
from app.models.course import Course  # noqa: E402
from app.models.english_requirement import EnglishRequirement  # noqa: E402
from app.models.evidence import ScrapedFieldEvidence  # noqa: E402
from app.models.fee import Fee  # noqa: E402
from app.models.scrape_runtime import ScrapeRuntimeJob, ScrapeRuntimeLog  # noqa: E402
from app.models.scraped_course import ScrapedCourse  # noqa: E402
from app.models.university import University  # noqa: E402
from app.services.scraper.config import loader as config_loader  # noqa: E402


_SECRET_KEY = re.compile(r"(token|secret|password|authorization|api[_-]?key)", re.I)
_URL = re.compile(r"https?://\S+", re.I)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_SECRET_VALUE = re.compile(
    r"(?i)\b(token|secret|password|authorization|api[_-]?key)\b\s*([:=]\s*|:\s*[^\s,;]+)"
)


def _json_value(value: Any) -> Any:
    """Convert DB values deterministically before hashing; never expose them."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def digest_row(row: dict[str, Any]) -> str:
    """SHA-256 of the complete row, without returning any row contents."""
    encoded = json.dumps(_json_value(row), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _safe_reason(value: Any) -> str | None:
    if value is None:
        return None
    value = _URL.sub("[url]", str(value))
    value = _EMAIL.sub("[email]", value)
    value = _SECRET_VALUE.sub(r"\1=[redacted]", value)
    return value[:500] or None


def _row_dicts(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings().all()]


def _config_evidence(university: dict[str, Any]) -> dict[str, Any]:
    """Load effective config without the loader's optional stub-writing branch."""
    scrape_url = university.get("scrape_url") or university.get("website") or ""
    host = urlparse(scrape_url).hostname or ""
    config = config_loader.get_config_for_host(
        hostname=host,
        name=university["name"],
        scrape_url=scrape_url,
        university_id=university["id"],
        db_scrape_config=university.get("scrape_config"),
        create_missing_stub=False,
    )
    data = config.model_dump(mode="json")
    slug = config_loader._hostname_to_slug(host)  # exact loader derivation
    yaml_path, id_specific = config_loader._select_uni_yaml(  # exact loader lookup
        slug=slug, university_id=university["id"], scrape_url=scrape_url
    )
    discovery = data.get("discovery", {})
    extraction = data.get("extraction", {})
    fees = extraction.get("fees", {}) if isinstance(extraction, dict) else {}
    degree = extraction.get("degree_level", {}) if isinstance(extraction, dict) else {}
    return {
        "identity": {
            "university_id": university["id"],
            "name": university["name"],
            "hostname": host,
            "slug": slug,
        },
        "yaml": {
            "resolved_path": str(yaml_path),
            "exists": yaml_path.exists(),
            "id_specific": id_specific,
            "runtime_override_path": str(config_loader._RUNTIME_UNIS_DIR / yaml_path.name),
        },
        "locked_recipe": {
            "locked_config_paths": config_loader._load_yaml_file(yaml_path).get("locked_config_paths", [])
            if yaml_path.exists() else [],
            "discovery": {
                key: discovery.get(key) for key in (
                    "allow_url_patterns", "block_url_patterns", "must_contain",
                    "skip_browser_discovery", "always_browser_discover",
                    "official_catalogue_fallback", "sitemap_url", "search_url",
                ) if key in discovery
            },
        },
        "extraction": {
            "currency": {
                "default_currency": fees.get("default_currency"),
                "currency_override": fees.get("currency_override"),
            },
            "degree_level_exception": degree,
        },
    }


def _compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare only stable identifiers/digests; staged additions are permitted."""
    old_rows = {r["id"]: r["sha256"] for r in baseline.get("scraped_courses", {}).get("rows", [])}
    now_rows = {r["id"]: r["sha256"] for r in current.get("scraped_courses", {}).get("rows", [])}
    old_courses = {r["id"]: r["sha256"] for r in baseline.get("published_courses", {}).get("rows", [])}
    now_courses = {r["id"]: r["sha256"] for r in current.get("published_courses", {}).get("rows", [])}
    def changed(old: dict[int, str], now: dict[int, str]) -> dict[str, list[int]]:
        return {
            "preserved_unchanged_ids": sorted(k for k in old.keys() & now.keys() if old[k] == now[k]),
            "changed_existing_ids": sorted(k for k in old.keys() & now.keys() if old[k] != now[k]),
            "missing_baseline_ids": sorted(old.keys() - now.keys()),
            "new_ids": sorted(now.keys() - old.keys()),
        }
    return {"scraped_courses": changed(old_rows, now_rows), "published_courses": changed(old_courses, now_courses)}


async def capture(university_id: int, job_id: str | None) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        # PostgreSQL enforces the intent even if this script regresses later.
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            university_rows = _row_dicts(await session.execute(
                select(University.__table__).where(University.id == university_id)
            ))
            if not university_rows:
                raise ValueError(f"University {university_id} was not found")
            university = university_rows[0]
            staged = _row_dicts(await session.execute(
                select(ScrapedCourse.__table__).where(ScrapedCourse.university_id == university_id)
            ))
            staged_ids = [row["id"] for row in staged]
            evidence = _row_dicts(await session.execute(
                select(ScrapedFieldEvidence.__table__).where(ScrapedFieldEvidence.scraped_course_id.in_(staged_ids))
            )) if staged_ids else []
            evidence_by_course: dict[int, list[str]] = {}
            for row in evidence:
                evidence_by_course.setdefault(row["scraped_course_id"], []).append(digest_row(row))
            published = _row_dicts(await session.execute(
                select(Course.__table__).where(Course.university_id == university_id)
            ))
            course_ids = [row["id"] for row in published]
            async def related(table: Any) -> dict[int, list[str]]:
                rows = _row_dicts(await session.execute(select(table).where(table.c.course_id.in_(course_ids)))) if course_ids else []
                grouped: dict[int, list[str]] = {}
                for row in rows:
                    grouped.setdefault(row["course_id"], []).append(digest_row(row))
                return grouped
            fee_digests = await related(Fee.__table__)
            english_digests = await related(EnglishRequirement.__table__)
            academic_digests = await related(AcademicRequirement.__table__)
            job: dict[str, Any] | None = None
            if job_id:
                jobs = _row_dicts(await session.execute(select(ScrapeRuntimeJob.__table__).where(
                    ScrapeRuntimeJob.runtime_job_id == job_id, ScrapeRuntimeJob.university_id == university_id
                )))
                if jobs:
                    row = jobs[0]
                    logs = _row_dicts(await session.execute(select(ScrapeRuntimeLog.__table__).where(
                        ScrapeRuntimeLog.runtime_job_id == job_id
                    ).order_by(ScrapeRuntimeLog.sequence)))
                    reasons: Counter[str] = Counter()
                    for log in logs:
                        payload = log.get("payload") or {}
                        for key in ("skip_reason", "error_reason", "reason", "error", "message"):
                            if key in payload and (reason := _safe_reason(payload[key])):
                                reasons[reason] += 1
                    gate_skip_counts = row.get("gate_skip_counts") or {}
                    stored_gate_skips = {
                        str(key)[:200]: int(value)
                        for key, value in gate_skip_counts.items()
                        if isinstance(value, (int, float))
                    } if isinstance(gate_skip_counts, dict) else {}
                    job = {"runtime_job_id": job_id, "status": row["status"], "discovered": row["total_found"],
                           "processed": row["current"], "imported": row["imported"], "skipped": row["skipped"],
                           "errors": row["errors"], "error_message": _safe_reason(row["error_message"]),
                           "stored_gate_skip_counts": stored_gate_skips,
                           "stored_skip_error_reasons": dict(reasons)}
                else:
                    job = {"runtime_job_id": job_id, "found": False}
            report = {
                "format": "full-review-evidence/v1",
                "read_only": True,
                "catalogue_certification_note": "Observed counts do not certify catalogue completeness; require completed job evidence.",
                "config": _config_evidence(university),
                "scraped_courses": {"count": len(staged), "rows": [
                    {"id": row["id"], "status": row["status"], "sha256": digest_row(row),
                     "evidence_sha256": sorted(evidence_by_course.get(row["id"], []))} for row in staged]},
                "published_courses": {"count": len(published), "rows": [
                    {"id": row["id"], "status": row["status"], "sha256": digest_row(row),
                     "fee_sha256": sorted(fee_digests.get(row["id"], [])),
                     "english_requirement_sha256": sorted(english_digests.get(row["id"], [])),
                     "academic_requirement_sha256": sorted(academic_digests.get(row["id"], []))} for row in published]},
                "job": job,
            }
            return report
        finally:
            await session.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture read-only, sanitized review evidence.")
    parser.add_argument("--university-id", type=int, required=True)
    parser.add_argument("--job-id", help="Optional scrape_runtime_jobs.runtime_job_id")
    parser.add_argument("--baseline", type=Path, help="Prior JSON report for ID/hash comparison")
    args = parser.parse_args()
    report = asyncio.run(capture(args.university_id, args.job_id))
    if args.baseline:
        with args.baseline.open(encoding="utf-8") as handle:
            report["baseline_comparison"] = _compare(json.load(handle), report)
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()