"""SRUC (Scotland's Rural College) — direct Umbraco JSON API provider.

SRUC's ``/course-catalogue/`` listing page is a JS SPA that renders course
"cards" client-side from a single Umbraco endpoint::

    GET /Umbraco/Api/CourseApi/GetCourses

The response shape is::

    {
      "pageTitle": ...,
      "featuredCourses": [...],
      "allCourses": [
        {
          "id": 44833,
          "courseTitle": "Agriculture",
          "description": "...",
          "filterIds": ["<campus-guid>", ..., "<studymode-guid>", ...],
          "qualifications": [
            {"name": "MA at SCQF level 5", "coursePageUrl": "/course-catalogue/agriculture/ma-agriculture-scqf-level-5/", "id": 44854, ...},
            {"name": "MA at SCQF level 6", "coursePageUrl": "...", ...},
            {"name": "BSc (Hons)", "coursePageUrl": "...", ...},
            {"name": "HNC", "coursePageUrl": "...", ...},
            {"name": "HND", "coursePageUrl": "...", ...},
            {"name": "NC", "coursePageUrl": "...", ...},
          ]
        },
        ...
      ],
      "filterOptions": [
        {"name": "Campuses", "values": [...]},
        {"name": "Study modes", "values": [{"id": "<guid>", "name": "Full Time"}, ...]},
        {"name": "Qualifications", "values": [...]},
      ]
    }

Each course "card" can show several qualification-level "capsule" buttons
(e.g. "MA at SCQF level 5", "BSc (Hons)", "HNC", "HND", "NC") — every capsule
is a DISTINCT course with its own ``coursePageUrl``. A naive one-link-per-card
discovery would only capture 51 parent course entries instead of the full set
of individual qualification pages.

The "Full time" filter shown on the front-end has no working server-side
query-string equivalent (``?filters=...`` on the HTML page and on the API
itself are both ignored — the API always returns every course regardless of
query params). The front-end applies the filter client-side by checking
whether the "Full Time" study-mode GUID (looked up from ``filterOptions``)
appears in each course's ``filterIds`` array. Filtering this way against a
live snapshot reproduces the operator-reported "38 full-time courses"
(51 total course cards, 38 of which include a Full Time offering; 68 of the
93 total qualification pages belong to those 38 courses).

This provider replicates that client-side filter, then flattens every
matching course's ``qualifications`` array into individual course links —
one link per capsule/qualification, not one per card.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from app.services.scraper.config.schema import SrucApiConfig

log = logging.getLogger("scraper.sruc_api")


def _find_study_mode_id(filter_options: list, mode_name: str) -> Optional[str]:
    """Look up the GUID for a named entry in the 'Study modes' filter group."""
    target = mode_name.strip().lower()
    for group in filter_options or []:
        if not isinstance(group, dict):
            continue
        if (group.get("name") or "").strip().lower() != "study modes":
            continue
        for value in group.get("values") or []:
            if not isinstance(value, dict):
                continue
            if (value.get("name") or "").strip().lower() == target:
                return value.get("id")
    return None


def _degree_level(qualification_name: str) -> str:
    """Map an SRUC qualification-capsule name to a canonical degree level.

    Values MUST match CANONICAL_DEGREE_LEVELS in extractors/degree_level.py.
    Unrecognised names return "" so stage_course.py re-infers from course_name
    rather than storing a non-canonical value.
    """
    n = qualification_name.lower().strip()
    if n.startswith("phd") or "doctor" in n:
        return "Doctorate"
    if n.startswith("msc") or n.startswith("ma ") or n == "ma" or n.startswith("meng"):
        return "Master's"
    if "pgdip" in n or "postgraduate diploma" in n:
        return "Graduate Diploma"
    if "pgcert" in n or "postgraduate certificate" in n:
        return "Graduate Certificate"
    if n.startswith("bsc") or n.startswith("ba ") or n == "ba" or n.startswith("beng"):
        return "Bachelor's"
    if n.startswith("hnd"):
        return "Diploma"
    if n.startswith("hnc"):
        return "Certificate"
    if n.startswith("nc"):
        return "Certificate"
    return ""


def _map_qualification(
    course: dict,
    qualification: dict,
    cfg: SrucApiConfig,
) -> Optional[dict]:
    """Build a discovery link dict for one qualification capsule.

    Returns a plain ``{name, url}`` link — no pre-populated payload — so the
    normal per-course HTML extraction pipeline runs on each qualification's
    own page (each capsule has its own distinct course-detail page).
    """
    raw_url = (qualification.get("coursePageUrl") or "").strip()
    if not raw_url:
        return None
    url = urljoin(cfg.base_url.rstrip("/") + "/", raw_url.lstrip("/"))

    course_title = (course.get("courseTitle") or "").strip()
    qual_name = (qualification.get("name") or "").strip()
    if course_title and qual_name:
        name = f"{course_title} - {qual_name}"
    else:
        name = course_title or qual_name
    if not name:
        return None

    return {"name": name, "url": url}


async def fetch_sruc_links(
    cfg: SrucApiConfig,
    emit=None,
) -> list[dict]:
    """Fetch SRUC's course-catalogue API, filter to the configured study mode,
    and flatten each matching course's qualifications into individual links.
    """
    endpoint = cfg.base_url.rstrip("/") + cfg.endpoint
    log.info("[SRUC_API] fetching %s", endpoint)
    if emit:
        emit("log", f"SRUC API: fetching {endpoint} …")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(endpoint, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("[SRUC_API] fetch failed: %s", exc)
        return []

    all_courses = data.get("allCourses") or []
    filter_options = data.get("filterOptions") or []
    log.info("[SRUC_API] allCourses=%d", len(all_courses))

    courses = all_courses
    if cfg.study_mode_filter:
        mode_id = _find_study_mode_id(filter_options, cfg.study_mode_filter)
        if mode_id is None:
            log.warning(
                "[SRUC_API] could not find study-mode filter %r in filterOptions — "
                "using all %d courses unfiltered",
                cfg.study_mode_filter, len(all_courses),
            )
        else:
            courses = [
                c for c in all_courses
                if isinstance(c, dict) and mode_id in (c.get("filterIds") or [])
            ]
            log.info(
                "[SRUC_API] filtered to study_mode=%r (id=%s): %d/%d courses",
                cfg.study_mode_filter, mode_id, len(courses), len(all_courses),
            )
            if emit:
                emit("log", f"SRUC API: {len(courses)}/{len(all_courses)} courses match '{cfg.study_mode_filter}'")

    links: list[dict] = []
    seen_urls: set[str] = set()
    skipped = 0
    for course in courses:
        if not isinstance(course, dict):
            continue
        for qualification in course.get("qualifications") or []:
            if not isinstance(qualification, dict):
                continue
            link = _map_qualification(course, qualification, cfg)
            if link is None:
                skipped += 1
                continue
            url = link["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            links.append(link)

    log.info(
        "[SRUC_API] built %d unique qualification links from %d courses (%d skipped/malformed)",
        len(links), len(courses), skipped,
    )
    if emit:
        emit("log", f"SRUC API: {len(links)} qualification links from {len(courses)} courses")
    return links
