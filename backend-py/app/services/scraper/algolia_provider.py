"""Generic Algolia search-API discovery provider.

Queries an Algolia index (paginated) and returns discovery links that feed
the normal per-course extraction pipeline. Optional configured provider
metadata is carried under ``payload`` and merged after HTML extraction.
No per-course HTML fetch is skipped.

Currently wired for Western Sydney University (wsu_prod_courses index)
but the implementation is university-agnostic — any YAML with a
``discovery.algolia`` block will use it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

import httpx

from app.services.scraper.config.schema import AlgoliaDiscoveryConfig

log = logging.getLogger("scraper.algolia_provider")

_ALGOLIA_BASE = "https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"
_WSU_ENGLISH_SOURCE = (
    "https://www.westernsydney.edu.au/international/studying/entry-requirements"
)
_WSU_ENGLISH_DEFAULT = {
    "ielts_overall": 6.5,
    "ielts_listening": 6.0,
    "ielts_reading": 6.0,
    "ielts_writing": 6.0,
    "ielts_speaking": 6.0,
    "toefl_overall": 82,
    "toefl_listening": 13,
    "toefl_reading": 13,
    "toefl_writing": 21,
    "toefl_speaking": 18,
    "pte_overall": 58,
    "pte_listening": 50,
    "pte_reading": 50,
    "pte_writing": 50,
    "pte_speaking": 50,
}


def _english_profile(
    *,
    ielts: tuple[float, float, float, float, float],
    toefl: tuple[int, int, int, int, int],
    pte: tuple[int, int, int, int, int],
) -> dict[str, float | int]:
    """Build slots in overall/listening/reading/writing/speaking order."""
    profile: dict[str, float | int] = {}
    suffixes = ("overall", "listening", "reading", "writing", "speaking")
    for prefix, values in (("ielts", ielts), ("toefl", toefl), ("pte", pte)):
        profile.update({
            f"{prefix}_{suffix}": value
            for suffix, value in zip(suffixes, values)
        })
    return profile


def _wsu_english_values(
    course_name: str,
    degree_level: str | None,
) -> tuple[dict[str, float | int], str] | None:
    """Return WSU's official standard or named-exception English profile."""
    from app.services.scraper.pathway_detection import is_pathway_program

    name = re.sub(r"\s+", " ", course_name).strip().lower()
    # WSU uses "(Pathway to Teaching ...)" in the names of complete bachelor
    # degrees. They are not preparatory/Foundation programs and remain subject
    # to the University's standard English profile.
    if is_pathway_program(course_name, degree_level) and not name.startswith(
        "bachelor of "
    ):
        return None

    if (
        name.startswith("bachelor of nursing")
        or "master of nursing practice (preregistration)" in name
    ):
        return _english_profile(
            ielts=(7.0, 7.0, 7.0, 7.0, 7.0),
            toefl=(94, 24, 24, 27, 23),
            pte=(65, 65, 65, 65, 65),
        ), "nursing"
    if any(marker in name for marker in (
        "bachelor of occupational therapy",
        "bachelor of paramedicine",
        "bachelor of physiotherapy",
        "bachelor of speech pathology",
        "bachelor of health science (sport & exercise science)",
        "bachelor of health science (sport and exercise science)",
    )):
        return _english_profile(
            ielts=(7.0, 7.0, 6.5, 6.5, 7.0),
            toefl=(94, 23, 24, 24, 23),
            pte=(65, 65, 58, 58, 65),
        ), "allied_health"
    if any(marker in name for marker in (
        "bachelor of clinical science (medicine)",
        "doctor of medicine",
        "bachelor of podiatric medicine",
    )):
        return _english_profile(
            ielts=(7.0, 7.0, 7.0, 7.0, 7.0),
            toefl=(100, 24, 24, 27, 23),
            pte=(65, 65, 65, 65, 65),
        ), "medicine"
    if (
        "bachelor of education (primary)" in name
        or name.startswith("master of teaching")
    ):
        return _english_profile(
            ielts=(7.5, 8.0, 7.0, 7.0, 8.0),
            toefl=(105, 28, 24, 24, 26),
            pte=(78, 79, 65, 65, 79),
        ), "teaching"
    if any(marker in name for marker in (
        "bachelor of social work",
        "bachelor of criminal and community justice/bachelor of social work",
        "master of social work (qualifying)",
    )):
        return _english_profile(
            ielts=(7.0, 7.0, 7.0, 7.0, 7.0),
            toefl=(94, 24, 24, 27, 23),
            pte=(65, 65, 65, 65, 65),
        ), "social_work"
    if (
        name.startswith("master of professional psychology")
        or name.startswith("master of clinical psychology")
    ):
        return _english_profile(
            ielts=(7.0, 7.0, 7.0, 7.0, 7.0),
            toefl=(100, 22, 22, 27, 22),
            pte=(65, 65, 65, 65, 65),
        ), "psychology"
    return dict(_WSU_ENGLISH_DEFAULT), "standard"


def _wsu_provider_values(raw: dict[str, Any]) -> list[tuple[str, Any, str, str]]:
    """Normalize authoritative fields from WSU's international Algolia index."""
    values: list[tuple[str, Any, str, str]] = []

    fee_text = str(raw.get("internationalFees") or "").strip()
    fee_match = re.search(r"(?:AUD\s*)?\$\s*([\d,]+(?:\.\d{1,2})?)", fee_text, re.I)
    if fee_match:
        fee = float(fee_match.group(1).replace(",", ""))
        fee = int(fee) if fee.is_integer() else fee
        values.extend([
            ("international_fee", fee, "internationalFees", fee_text),
            ("fee_currency", "AUD", "internationalFees", fee_text),
            ("fee_term", "Annual", "internationalFees", fee_text),
        ])

    duration_text = str(raw.get("duration") or "").strip()
    duration_match = re.search(
        r"\bFull\s*Time\s*:\s*(\d+(?:\.\d+)?)\s*(Years?|Months?|Weeks?)\b",
        duration_text,
        re.I,
    )
    if duration_match:
        duration = float(duration_match.group(1))
        duration = int(duration) if duration.is_integer() else duration
        unit = duration_match.group(2).lower()
        term = "Years" if unit.startswith("year") else (
            "Months" if unit.startswith("month") else "Weeks"
        )
        values.extend([
            ("duration", duration, "duration", duration_text),
            ("duration_term", term, "duration", duration_text),
        ])

    campuses = [
        str(value).strip()
        for value in (raw.get("campuses") or [])
        if str(value).strip() and str(value).strip().lower() != "online"
    ]
    if campuses:
        values.append(("course_location", ", ".join(dict.fromkeys(campuses)), "campuses", ", ".join(campuses)))

    months: list[str] = []
    for session in raw.get("intakeSessions") or []:
        if not isinstance(session, dict):
            continue
        date_text = str(session.get("startDate") or "").strip()
        try:
            month = datetime.strptime(date_text, "%d %B %Y").strftime("%B")
        except ValueError:
            name_match = re.search(
                r"\b(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\b",
                f"{session.get('name') or ''} {date_text}",
                re.I,
            )
            month = name_match.group(1).title() if name_match else ""
        if month and month not in months:
            months.append(month)
    if months:
        values.append(("intake_months", months, "intakeSessions", ", ".join(months)))

    cricos = str(raw.get("cricosCode") or "").strip()
    if cricos:
        values.append(("cricos_code", cricos, "cricosCode", cricos))
    return values


def merge_algolia_payload(
    extraction_result: dict[str, Any],
    provider_payload: dict[str, Any],
    *,
    url: str,
) -> dict[str, Any]:
    """Merge configured Algolia metadata into a completed HTML extraction."""
    if provider_payload.get("_provider") != "algolia":
        return extraction_result
    if "westernsydney.edu.au" not in url.lower():
        return extraction_result

    payload = extraction_result.setdefault("payload", {})
    evidence = extraction_result.setdefault("evidence", [])
    provider_source = str(
        provider_payload.get("_source_url") or _WSU_ENGLISH_SOURCE
    ).strip()
    authoritative = {
        "international_fee",
        "fee_currency",
        "fee_term",
        "duration",
        "duration_term",
    }
    for field, value, source_field, snippet in _wsu_provider_values(provider_payload):
        if value in (None, "", []):
            continue
        if field not in authoritative and payload.get(field) not in (None, "", []):
            continue
        if payload.get(field) == value:
            continue
        if field in authoritative:
            for prior in evidence:
                if (
                    prior.get("field_key") == field
                    and prior.get("decision_status") != "superseded"
                ):
                    prior["decision_status"] = "superseded"
        payload[field] = value
        evidence.append({
            "field_key": field,
            "value": value,
            "normalized": value,
            "source_url": provider_source,
            "page_type": "api",
            "method": f"algolia:{source_field}",
            "snippet": snippet,
            "confidence": 0.98,
            "decision_status": "selected",
        })

    course_name = str(
        payload.get("course_name")
        or provider_payload.get("_course_name")
        or ""
    ).strip()
    english = _wsu_english_values(
        course_name,
        str(payload.get("degree_level") or "").strip() or None,
    )
    if english is not None:
        english_values, profile_name = english
        for field, value in english_values.items():
            if payload.get(field) not in (None, "", 0):
                continue
            payload[field] = value
            evidence.append({
                "field_key": field,
                "value": value,
                "normalized": value,
                "source_url": _WSU_ENGLISH_SOURCE,
                "page_type": "central_english",
                "method": f"wsu:central_english:{profile_name}",
                "snippet": (
                    "Western Sydney University international English "
                    f"requirements ({profile_name}): {field}={value}"
                ),
                "confidence": 0.98,
                "decision_status": "selected",
            })
    return extraction_result


async def fetch_algolia_links(
    cfg: AlgoliaDiscoveryConfig,
    emit: Callable[..., Coroutine[Any, Any, None]],
) -> list[dict]:
    """Paginate through an Algolia index and return discovery link dicts.

    Each dict has the shape ``{name: str, url: str}`` which is all the
    orchestrator needs to feed per-course extraction.

    Args:
        cfg:  AlgoliaDiscoveryConfig loaded from the per-uni YAML.
        emit: async SSE emitter (same signature as in orchestrator).

    Returns:
        List of ``{name, url}`` dicts (deduplicated by URL).
    """
    endpoint = _ALGOLIA_BASE.format(app_id=cfg.app_id, index=cfg.index_name)
    headers = {
        "X-Algolia-Application-Id": cfg.app_id,
        "X-Algolia-API-Key": cfg.api_key,
        "Content-Type": "application/json",
    }

    facet_filters: list[list[str]] = []
    if cfg.facet_filter:
        facet_filters = [[cfg.facet_filter]]

    links: list[dict] = []
    seen_urls: set[str] = set()
    page = 0
    total_pages: Optional[int] = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            attributes = list(dict.fromkeys([
                cfg.url_field,
                cfg.name_field,
                *cfg.payload_fields,
            ]))
            body = {
                "query": "",
                "hitsPerPage": cfg.hits_per_page,
                "page": page,
                "attributesToRetrieve": attributes,
            }
            if facet_filters:
                body["facetFilters"] = facet_filters

            log.info(
                "[ALGOLIA] querying index=%s page=%d facet=%s",
                cfg.index_name, page, cfg.facet_filter or "(none)",
            )
            try:
                resp = await client.post(endpoint, headers=headers, content=json.dumps(body))
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[ALGOLIA] page %d fetch failed: %s", page, exc)
                await emit(
                    "status",
                    f"[DISCOVER] Algolia: page {page} fetch failed — {exc}",
                    phase="discover",
                )
                break

            if total_pages is None:
                total_pages = data.get("nbPages", 1)
                total_hits = data.get("nbHits", 0)
                log.info(
                    "[ALGOLIA] index=%s total_hits=%d total_pages=%d",
                    cfg.index_name, total_hits, total_pages,
                )
                await emit(
                    "status",
                    f"[DISCOVER] Algolia: {total_hits} courses found across {total_pages} page(s)",
                    phase="discover",
                )

            hits = data.get("hits", [])
            for hit in hits:
                url = (hit.get(cfg.url_field) or hit.get("url") or "").strip()
                name = (hit.get(cfg.name_field) or "").strip()
                if not url or not url.startswith("http"):
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                link = {"name": name, "url": url}
                if cfg.payload_fields:
                    link["payload"] = {
                        "_provider": "algolia",
                        "_course_name": name,
                        "_source_url": endpoint,
                        **{
                            field: hit.get(field)
                            for field in cfg.payload_fields
                            if hit.get(field) not in (None, "", [])
                        },
                    }
                links.append(link)

            page += 1
            if page >= (total_pages or 1):
                break

    log.info("[ALGOLIA] discovery complete: %d unique course links", len(links))
    await emit(
        "status",
        f"[DISCOVER] Algolia: {len(links)} unique course links extracted",
        phase="discover",
    )
    return links
