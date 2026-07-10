"""Unit tests for sruc_api.py — SRUC Umbraco JSON API discovery provider.

Focus: the capsule-flattening logic (each qualification under a course is a
distinct course link) and the client-side "Full time" filterIds replication,
since SRUC's API has no working server-side filter query param.
"""
from __future__ import annotations

import pytest

from app.services.scraper.config.schema import SrucApiConfig
from app.services.scraper.sruc_api import (
    _degree_level,
    _find_study_mode_id,
    _map_qualification,
    fetch_sruc_links,
)


def _cfg(**overrides) -> SrucApiConfig:
    defaults = dict(enabled=True, base_url="https://www.sruc.ac.uk")
    defaults.update(overrides)
    return SrucApiConfig(**defaults)


_FILTER_OPTIONS = [
    {
        "name": "Campuses",
        "values": [{"id": "campus-1", "name": "Ayr"}],
    },
    {
        "name": "Study modes",
        "values": [
            {"id": "ft-guid", "name": "Full Time"},
            {"id": "pt-guid", "name": "Part Time"},
        ],
    },
]

_COURSE_FULL_TIME = {
    "id": 1,
    "courseTitle": "Agriculture",
    "filterIds": ["campus-1", "ft-guid"],
    "qualifications": [
        {"name": "MA at SCQF level 5", "coursePageUrl": "/course-catalogue/agriculture/ma-agriculture-scqf-level-5/"},
        {"name": "MA at SCQF level 6", "coursePageUrl": "/course-catalogue/agriculture/ma-agriculture-scqf-level-6/"},
        {"name": "BSc (Hons)", "coursePageUrl": "/course-catalogue/agriculture/bsc-hons-agriculture/"},
        {"name": "HNC", "coursePageUrl": "/course-catalogue/agriculture/hnc-agriculture/"},
        {"name": "HND", "coursePageUrl": "/course-catalogue/agriculture/hnd-agriculture/"},
        {"name": "NC", "coursePageUrl": "/course-catalogue/agriculture/nc-agriculture/"},
    ],
}

_COURSE_PART_TIME_ONLY = {
    "id": 2,
    "courseTitle": "Evening Studies",
    "filterIds": ["campus-1", "pt-guid"],
    "qualifications": [
        {"name": "Certificate", "coursePageUrl": "/course-catalogue/evening-studies/certificate/"},
    ],
}


# ── _find_study_mode_id ───────────────────────────────────────────────────────

def test_find_study_mode_id_matches_case_insensitive():
    assert _find_study_mode_id(_FILTER_OPTIONS, "full time") == "ft-guid"
    assert _find_study_mode_id(_FILTER_OPTIONS, "Full Time") == "ft-guid"


def test_find_study_mode_id_missing_returns_none():
    assert _find_study_mode_id(_FILTER_OPTIONS, "Distance Learning") is None


def test_find_study_mode_id_ignores_non_study_mode_groups():
    assert _find_study_mode_id(_FILTER_OPTIONS, "Ayr") is None


# ── _degree_level ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("MA at SCQF level 5", "Master's"),
    ("MA at SCQF level 6", "Master's"),
    ("BSc (Hons)", "Bachelor's"),
    ("HNC", "Certificate"),
    ("HND", "Diploma"),
    ("NC", "Certificate"),
    ("NC Introduction", "Certificate"),
    ("PhD", "Doctorate"),
    ("PgDip", "Graduate Diploma"),
    ("PgCert", "Graduate Certificate"),
    ("College Certificate", ""),
])
def test_degree_level_mapping(raw, expected):
    assert _degree_level(raw) == expected


# ── _map_qualification (capsule flattening) ──────────────────────────────────

def test_map_qualification_builds_absolute_url_and_combined_name():
    cfg = _cfg()
    qual = _COURSE_FULL_TIME["qualifications"][2]  # BSc (Hons)
    link = _map_qualification(_COURSE_FULL_TIME, qual, cfg)
    assert link["url"] == "https://www.sruc.ac.uk/course-catalogue/agriculture/bsc-hons-agriculture/"
    assert link["name"] == "Agriculture - BSc (Hons)"


def test_map_qualification_returns_none_without_url():
    cfg = _cfg()
    link = _map_qualification(_COURSE_FULL_TIME, {"name": "HNC", "coursePageUrl": ""}, cfg)
    assert link is None


# ── fetch_sruc_links (end-to-end with mocked httpx) ──────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload, *args, **kwargs):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_fetch_sruc_links_flattens_capsules_and_filters_full_time(monkeypatch):
    payload = {
        "allCourses": [_COURSE_FULL_TIME, _COURSE_PART_TIME_ONLY],
        "filterOptions": _FILTER_OPTIONS,
    }

    import app.services.scraper.sruc_api as sruc_api_mod
    monkeypatch.setattr(
        sruc_api_mod.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(payload, *a, **kw),
    )

    links = await fetch_sruc_links(_cfg(study_mode_filter="Full Time"))

    # Only the full-time course's 6 qualification capsules should be returned;
    # the part-time-only course must be excluded entirely.
    assert len(links) == 6
    urls = {l["url"] for l in links}
    assert "https://www.sruc.ac.uk/course-catalogue/evening-studies/certificate/" not in urls
    assert "https://www.sruc.ac.uk/course-catalogue/agriculture/hnd-agriculture/" in urls


@pytest.mark.asyncio
async def test_fetch_sruc_links_no_filter_includes_all_courses(monkeypatch):
    payload = {
        "allCourses": [_COURSE_FULL_TIME, _COURSE_PART_TIME_ONLY],
        "filterOptions": _FILTER_OPTIONS,
    }

    import app.services.scraper.sruc_api as sruc_api_mod
    monkeypatch.setattr(
        sruc_api_mod.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(payload, *a, **kw),
    )

    links = await fetch_sruc_links(_cfg(study_mode_filter=None))
    assert len(links) == 7  # 6 + 1


@pytest.mark.asyncio
async def test_fetch_sruc_links_dedupes_by_url(monkeypatch):
    dup_course = dict(_COURSE_FULL_TIME)
    payload = {
        "allCourses": [_COURSE_FULL_TIME, dup_course],
        "filterOptions": _FILTER_OPTIONS,
    }

    import app.services.scraper.sruc_api as sruc_api_mod
    monkeypatch.setattr(
        sruc_api_mod.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(payload, *a, **kw),
    )

    links = await fetch_sruc_links(_cfg(study_mode_filter="Full Time"))
    assert len(links) == 6


@pytest.mark.asyncio
async def test_fetch_sruc_links_unknown_filter_falls_back_to_all(monkeypatch):
    payload = {
        "allCourses": [_COURSE_FULL_TIME, _COURSE_PART_TIME_ONLY],
        "filterOptions": _FILTER_OPTIONS,
    }

    import app.services.scraper.sruc_api as sruc_api_mod
    monkeypatch.setattr(
        sruc_api_mod.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(payload, *a, **kw),
    )

    links = await fetch_sruc_links(_cfg(study_mode_filter="Nonexistent Mode"))
    assert len(links) == 7  # falls back to unfiltered rather than dropping everything
