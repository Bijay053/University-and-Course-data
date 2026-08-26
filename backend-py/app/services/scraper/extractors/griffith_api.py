"""Authoritative Griffith degree extraction from the public program API."""
from __future__ import annotations

import re
import time
from html import escape
from typing import Any
from urllib.parse import urlparse

import httpx

_PROGRAM_CODE_RE = re.compile(r"-(\d+)/?$")
_INTAKE_MONTHS = {
    "trimester 1": "March",
    "trimester 2": "July",
    "trimester 3": "November",
    "summer semester": "November",
}
_PROGRAM_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_S = 1800.0
_FUNNELBACK_URL = "https://dxp-au-search.funnelback.squiz.cloud/s/search.json"


def is_griffith_degree_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.hostname in {"griffith.edu.au", "www.griffith.edu.au"}
        and parsed.path.startswith("/study/degrees/")
        and _PROGRAM_CODE_RE.search(parsed.path) is not None
    )


def program_code_from_url(url: str) -> str | None:
    match = _PROGRAM_CODE_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def _display_title(title: str) -> str:
    """Match the qualification/title split used by Griffith's visible banner."""
    title = re.sub(r"\s*/\s*", "/", (title or "").strip())
    qualifications = re.findall(
        r"Bachelor of|Graduate Certificate in|Undergraduate Certificate in|"
        r"Graduate Diploma of|Diploma of|Executive Master of|Master of|Doctor of",
        title,
    )
    if not qualifications:
        return title.replace("/", " / ")

    prefix = "/".join(qualifications)
    if len(qualifications) > 1 and len(set(qualifications)) == 1:
        prefix = qualifications[0].replace(" of", "s of")

    short = re.sub(
        r"Bachelor of |Graduate Certificate in |Undergraduate Certificate in |"
        r"Graduate Diploma of |Diploma of |Executive Master of |Master of |Doctor of ",
        "",
        title,
    )
    return f"{prefix} {short.replace('/', ' / ')}".strip()


def _international_fee(doc: dict[str, Any]) -> tuple[float | None, int | None]:
    candidates: list[tuple[int, float]] = []
    for year_group in doc.get("knownFees") or []:
        year = int(year_group.get("year") or 0)
        for fee in year_group.get("fees") or []:
            fee_type = ((fee.get("band") or {}).get("feeType") or {})
            if str(fee_type.get("code") or "").upper() != "INTL":
                continue
            try:
                amount = float(fee.get("amount"))
            except (TypeError, ValueError):
                continue
            if amount > 0:
                candidates.append((year, amount))
    if not candidates:
        return None, None
    year, amount = max(candidates, key=lambda item: item[0])
    return amount, year


def map_program(doc: dict[str, Any]) -> dict[str, Any]:
    """Map a Griffith program response to fields supported by ScrapedCourse."""
    mapped: dict[str, Any] = {}

    title = str(doc.get("title") or "").strip()
    if title:
        mapped["course_name"] = _display_title(title)

    campuses = [
        str(item.get("name") or "").strip()
        for item in doc.get("campuses") or []
        if str(item.get("name") or "").strip()
    ]
    physical = [name for name in campuses if name.lower() != "distance education"]
    has_online = len(physical) != len(campuses)
    if physical:
        mapped["course_location"] = ", ".join(dict.fromkeys(physical))
        mapped["study_mode"] = "Mixed" if has_online else "On Campus"
    elif has_online:
        mapped["study_mode"] = "Online"

    for duration in doc.get("duration") or []:
        if str(duration.get("studentType") or "").lower() != "international":
            continue
        try:
            value = float(duration.get("fullTime"))
        except (TypeError, ValueError):
            break
        if value > 0:
            mapped["duration"] = value
            mapped["duration_term"] = "Year"
            mapped["study_load"] = "Full Time"
        break

    fee, fee_year = _international_fee(doc)
    if fee is not None:
        mapped.update(
            international_fee=fee,
            fee_term="Annual",
            fee_year=fee_year,
            currency="AUD",
        )

    intakes = []
    for item in doc.get("intakes") or []:
        if not item.get("isInternational"):
            continue
        month = _INTAKE_MONTHS.get(str(item.get("name") or "").strip().lower())
        if month and month not in intakes:
            intakes.append(month)
    if intakes:
        mapped["intake_months"] = intakes

    scores = []
    for detail in doc.get("admissionDetails") or []:
        try:
            score = float(detail.get("englishLanguageScore"))
        except (TypeError, ValueError):
            continue
        if 4.0 <= score <= 9.0:
            scores.append(score)
    if scores:
        mapped["ielts_overall"] = max(scores)

    cricos = str(doc.get("cricos") or "").strip().upper()
    if re.fullmatch(r"\d{6}[A-Z]", cricos):
        mapped["cricos_code"] = cricos

    career = str((doc.get("academicCareer") or {}).get("name") or "").strip()
    if career:
        mapped["degree_level"] = career

    overview = next(
        (
            item
            for item in doc.get("programOverviews") or []
            if str(item.get("studentType") or "").lower() == "international"
        ),
        None,
    )
    overview = overview or next(iter(doc.get("programOverviews") or []), None)
    if overview:
        description = re.sub(
            r"<[^>]+>", " ", str(overview.get("overview") or "")
        )
        description = re.sub(r"\s+", " ", description).strip()
        if description:
            mapped["description"] = description

    requirement = doc.get("internationalEntryRequirement") or {}
    pathway = re.sub(r"<[^>]+>", " ", str(requirement.get("pathway") or ""))
    pathway = re.sub(r"\s+", " ", pathway).strip()
    if pathway:
        mapped["other_requirement"] = pathway

    return mapped


def program_document_html(doc: dict[str, Any]) -> str:
    """Build a substantive document so Griffith can bypass its costly SPA shell."""
    mapped = map_program(doc)
    lines = [
        f"<h1>{escape(str(mapped.get('course_name') or doc.get('title') or ''))}</h1>",
        "<h2>International student key facts</h2>",
    ]
    for field, value in mapped.items():
        lines.append(
            f"<p><strong>{escape(field.replace('_', ' ').title())}:</strong> "
            f"{escape(str(value))}</p>"
        )
    lines.append(
        "<p>This structured course record is supplied by Griffith University's "
        "public degree program API for international applicants.</p>"
    )
    return "<html><body>" + "".join(lines) + "</body></html>"


def search_result_to_program(result: dict[str, Any], code: str) -> dict[str, Any]:
    """Adapt Griffith's search-index metadata when a retired API record is 404."""
    meta = result.get("listMetadata") or {}

    def values(key: str) -> list[Any]:
        value = meta.get(key) or []
        return value if isinstance(value, list) else [value]

    title = next(iter(values("title")), None) or result.get("title")
    campuses = [
        {
            "name": "Distance Education" if str(name).lower() == "online" else name,
            "code": "",
        }
        for name in values("campusName")
        if name
    ]

    full_time = None
    duration_values = values("duration")
    for index, value in enumerate(duration_values):
        if str(value).lower() == "international" and index + 2 < len(duration_values):
            full_time = re.sub(r"\s*\([^)]*\)\s*", "", str(duration_values[index + 2]))
            break

    intakes = []
    names = values("intakesName")
    international_flags = values("intakesIsInternational")
    for index, name in enumerate(names):
        is_international = (
            index >= len(international_flags)
            or str(international_flags[index]).lower() == "true"
        )
        intakes.append(
            {"name": name, "isDomestic": False, "isInternational": is_international}
        )

    fee = next(iter(values("intlFee")), None)
    fee_year = next(iter(values("knownFeesYear")), None)
    known_fees = []
    if fee:
        known_fees = [
            {
                "year": int(fee_year or 0),
                "fees": [
                    {
                        "amount": float(fee),
                        "band": {"feeType": {"code": "INTL"}},
                    }
                ],
            }
        ]

    descriptions = values("description")
    student_types = values("descriptionStudentType")
    description = descriptions[-1] if descriptions else ""
    if "International" in student_types:
        description = descriptions[student_types.index("International")]

    requirement = next(iter(values("requirementsSummary")), "")
    return {
        "code": code,
        "title": title,
        "campuses": campuses,
        "duration": [
            {
                "studentType": "International",
                "fullTime": full_time,
                "partTime": None,
            }
        ],
        "knownFees": known_fees,
        "intakes": intakes,
        "academicCareer": {
            "name": next(iter(values("academicCareer")), ""),
            "code": next(iter(values("academicCareerCode")), ""),
        },
        "programOverviews": [
            {"studentType": "International", "overview": description}
        ],
        "internationalEntryRequirement": {"pathway": requirement},
    }


async def _fetch_search_fallback(client: httpx.AsyncClient, code: str) -> dict[str, Any]:
    response = await client.get(
        _FUNNELBACK_URL,
        params={
            "collection": "griff~sp-degrees-api",
            "profile": "degrees",
            "f.Available to|studentType": "intl",
            "smeta_intlCohortYears_orsand": "2027",
            "start_rank": 1,
        },
    )
    response.raise_for_status()
    results = (
        ((response.json().get("response") or {}).get("resultPacket") or {}).get("results")
        or []
    )
    result = next(
        (
            item
            for item in results
            if program_code_from_url(str(item.get("liveUrl") or "")) == code
        ),
        None,
    )
    if not result:
        raise ValueError(f"Griffith search index has no fallback record for {code}")
    return search_result_to_program(result, code)


async def fetch_program(url: str) -> dict[str, Any]:
    code = program_code_from_url(url)
    if not code:
        raise ValueError(f"Could not derive Griffith program code from {url}")
    cached = _PROGRAM_CACHE.get(code)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]
    endpoint = f"https://degrees.griffith.edu.au/rest-api/v3/program/{code}"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    ) as client:
        response = await client.get(endpoint)
        if response.status_code == 404:
            data = await _fetch_search_fallback(client, code)
        else:
            response.raise_for_status()
            data = response.json()
    if not isinstance(data, dict) or str(data.get("code") or "") != code:
        raise ValueError(f"Invalid Griffith program response for {code}")
    _PROGRAM_CACHE[code] = (time.monotonic(), data)
    return data


async def apply_overrides(
    payload: dict[str, Any],
    *,
    url: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    mapped = map_program(await fetch_program(url))
    if not mapped:
        raise ValueError(f"Griffith program API returned no supported fields for {url}")
    for field, value in mapped.items():
        previous = payload.get(field)
        payload[field] = value
        evidence.append(
            {
                "field_key": field,
                "value": value,
                "confidence": 0.99,
                "method": "griffith.program_api",
                "snippet": f"Griffith program API: {field}={value!r}",
            }
        )
    return mapped