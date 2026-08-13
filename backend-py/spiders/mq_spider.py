"""Macquarie University spider — Funnelback JSON API + Gatsby page-data.json.

Discovery + extraction in two steps:

Step 1 — Funnelback search API
  GET https://mqu-search.funnelback.squiz.cloud/s/search.json
      ?collection=mqu~sp-courses&profile=international
      &query=!padrenull&start_rank=1&num_ranks=338

  Returns all 338 international courses as JSON.
  Each result has:
    result["title"]               → course name
    result["liveUrl"]             → real admissions URL
    result["metaData"]["studyLevel"]    → e.g. "Undergraduate"
    result["metaData"]["courseDuration"] → e.g. "3 years"

Step 2 — Gatsby page-data.json per course
  URL: liveUrl.replace(".au/study/", ".au/study/page-data/") + "/page-data.json"

  Inner JSON at: result.data.current.fields.json  (JSON-encoded string, parse again)

  Fields used:
    fees[].fee_type.label           → filter "international"
    fees[].estimated_annual_fee     → international_fee (AUD)
    ielts_overall_score             → ielts_overall
    ielts_reading_score             → ielts_reading
    ielts_writing_score             → ielts_writing
    ielts_listening_score           → ielts_listening
    ielts_speaking_score            → ielts_speaking
    admission_requirements          → other_requirement
    marketing_items.descriptions[].long_description → description
    marketing_items.employments     → career info
    enrolment_patterns              → study_load / study_mode
    offering[].location             → course_location + study_mode
    offering[].language_of_instruction.label → language

Yields rich items (payload + evidence) so the orchestrator's _extract_only
short-circuit returns them verbatim — no per-course browser or HTML fetch.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import scrapy

# ─────────────────────────────────────────────────────────────────────────────

_UNI_NAME  = "Macquarie University"
_CURRENCY  = "AUD"
_FEE_YEAR  = "2026"
_LOCATION  = "North Ryde"   # default campus; overridden per course from offering[]

_FUNNELBACK_BASE = (
    "https://mqu-search.funnelback.squiz.cloud/s/search.json"
    "?collection=mqu~sp-courses&profile=international"
    "&query=!padrenull"
)
_PAGE_SIZE = 200   # server-side cap; request this many per page

# ─────────────────────────────────────────────────────────────────────────────


class MQSpider(scrapy.Spider):
    """Macquarie University international course spider."""

    name = "mq_spider"

    # First page — Scrapy fetches this automatically; parse() handles pagination.
    start_urls = [f"{_FUNNELBACK_BASE}&start_rank=1&num_ranks={_PAGE_SIZE}"]

    custom_settings = {
        "DOWNLOAD_DELAY": 0.5,
        "CONCURRENT_REQUESTS": 8,
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "application/json, */*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_5) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/55.0.2883.95 Safari/537.36"
            ),
        },
    }

    # ── Step 1: parse Funnelback JSON response (paginated) ───────────────────

    def parse(self, response):
        """Parse Funnelback search API response → schedule page-data.json fetches.

        The endpoint caps results at 200 per request regardless of num_ranks.
        We read resultsSummary.nextStart and follow subsequent pages until
        nextStart is absent (last page).
        """
        try:
            data = json.loads(response.text)
        except Exception as exc:
            self.logger.error("MQ: failed to parse Funnelback JSON: %s", exc)
            return

        packet = data.get("response", {}).get("resultPacket", {})
        results = packet.get("results", [])
        summary = packet.get("resultsSummary", {})

        self.logger.info(
            "MQ: Funnelback page start_rank=%s → %d results (fullyMatching=%s)",
            summary.get("currStart"), len(results), summary.get("fullyMatching"),
        )

        # ── paginate: follow nextStart if present ─────────────────────────
        next_start = summary.get("nextStart")
        if next_start:
            next_url = f"{_FUNNELBACK_BASE}&start_rank={next_start}&num_ranks={_PAGE_SIZE}"
            yield scrapy.Request(next_url, callback=self.parse, dont_filter=True)

        for program in results:
            course_info: dict[str, Any] = {}

            try:
                course_info["course_name"] = program["title"]
            except (KeyError, TypeError):
                pass

            try:
                course_info["course_website"] = program["liveUrl"]
            except (KeyError, TypeError):
                pass

            try:
                meta = program["metaData"]
                try:
                    course_info["degree"] = meta["studyLevel"]
                except (KeyError, TypeError):
                    pass
                try:
                    course_info["duration"] = meta["courseDuration"]
                    course_info["duration_term"] = "year"
                except (KeyError, TypeError):
                    pass
            except (KeyError, TypeError):
                pass

            live_url = course_info.get("course_website", "").rstrip("/")
            if not live_url:
                continue

            # Construct page-data.json URL (Gatsby static data endpoint)
            page_data_url = (
                live_url.replace(".au/study/", ".au/study/page-data/")
                + "/page-data.json"
            )

            yield scrapy.Request(
                url=page_data_url,
                callback=self.parse_page_data,
                cb_kwargs={"course_info": course_info},
                errback=self.handle_error,
                dont_filter=True,
            )

    # ── Step 2: parse Gatsby page-data.json ──────────────────────────────────

    def parse_page_data(self, response, course_info: dict):
        """Extract fee, IELTS, description etc. from Gatsby page-data.json."""
        name = course_info.get("course_name") or response.url
        url  = course_info.get("course_website") or response.url

        try:
            outer = json.loads(response.text)
            program = json.loads(
                outer["result"]["data"]["current"]["fields"]["json"]
            )
        except Exception as exc:
            self.logger.warning(
                "MQ: failed to parse page-data.json for %s: %s", url, exc
            )
            # Yield discovery-only item so the orchestrator can fall back to
            # normal HTML extraction for this course.
            yield {"name": name, "url": url}
            return

        payload: dict[str, Any] = {
            "course_name": name,
            "course_website": url,
        }
        evidence: list[dict] = [
            _ev("course_name", name, "scrapy:funnelback:title", url,
                f"Funnelback result title: {name}", 0.95),
        ]

        # ── degree / study level from Funnelback metaData ─────────────────
        degree_raw = course_info.get("degree", "")
        if degree_raw:
            payload["degree_level"] = degree_raw
            acad = _academic_level(degree_raw)
            if acad:
                payload["academic_level"] = acad
            evidence.append(_ev(
                "degree_level", degree_raw, "scrapy:funnelback:studyLevel", url,
                f"Funnelback metaData.studyLevel: {degree_raw}", 0.85,
            ))

        # ── duration ──────────────────────────────────────────────────────
        dur_raw = course_info.get("duration", "")
        dur_val, dur_unit = _parse_duration(dur_raw)
        if dur_val is not None:
            payload["duration"] = dur_val
            payload["duration_term"] = dur_unit
            evidence.append(_ev(
                "duration", dur_val, "scrapy:funnelback:courseDuration", url,
                f"Funnelback metaData.courseDuration: {dur_raw}", 0.75,
            ))

        # ── international fee ─────────────────────────────────────────────
        fees = program.get("fees") or []
        for fee_item in fees:
            label = ((fee_item.get("fee_type") or {}).get("label") or "").lower()
            if "international" in label:
                raw_fee = fee_item.get("estimated_annual_fee")
                try:
                    fee_val = float(raw_fee)
                    payload["international_fee"] = fee_val
                    payload["fee_term"] = "Year"
                    payload["fee_year"] = _FEE_YEAR
                    payload["currency"] = _CURRENCY
                    evidence.append(_ev(
                        "international_fee", fee_val,
                        "scrapy:page_data:fees", url,
                        f"fees[].estimated_annual_fee (international): {raw_fee}", 0.90,
                    ))
                except (TypeError, ValueError):
                    pass
                break

        # ── IELTS ─────────────────────────────────────────────────────────
        _ielts_map = {
            "ielts_overall_score":   "ielts_overall",
            "ielts_reading_score":   "ielts_reading",
            "ielts_writing_score":   "ielts_writing",
            "ielts_listening_score": "ielts_listening",
            "ielts_speaking_score":  "ielts_speaking",
        }
        for src, dst in _ielts_map.items():
            raw = program.get(src)
            if raw is not None:
                try:
                    score = float(raw)
                    payload[dst] = score
                    evidence.append(_ev(
                        dst, score, "scrapy:page_data:ielts", url,
                        f"page-data.json {src}: {raw}", 0.90,
                    ))
                except (TypeError, ValueError):
                    pass

        # ── admission requirements ────────────────────────────────────────
        admission_req = program.get("admission_requirements")
        if admission_req:
            payload["other_requirement"] = admission_req
            evidence.append(_ev(
                "other_requirement", str(admission_req)[:80],
                "scrapy:page_data:admission_requirements", url,
                "page-data.json admission_requirements", 0.80,
            ))

        # ── description from marketing_items.descriptions ─────────────────
        try:
            descs = (program.get("marketing_items") or {}).get("descriptions") or []
            parts = [d["long_description"] for d in descs if d.get("long_description")]
            if parts:
                payload["description"] = "\n".join(parts)
                evidence.append(_ev(
                    "description", payload["description"][:80] + "…",
                    "scrapy:page_data:marketing_descriptions", url,
                    "page-data.json marketing_items.descriptions", 0.80,
                ))
        except Exception:
            pass

        # ── study load from enrolment_patterns ───────────────────────────
        try:
            patterns = program.get("enrolment_patterns") or []
            if "Part Time" in patterns and "Full Time" in patterns:
                payload["study_load"] = "Full Time"
            elif "Part Time" in patterns:
                payload["study_load"] = "Part Time"
            elif "Full Time" in patterns:
                payload["study_load"] = "Full Time"
        except Exception:
            pass

        # ── location + study mode from offering[] ─────────────────────────
        try:
            offerings = program.get("offering") or []
            locations: set[str] = set()
            for of in offerings:
                loc = (of.get("location") or "").strip()
                if loc:
                    locations.add(loc)
            if locations:
                if "Off-campus" in locations and len(locations) == 1:
                    study_mode = "Online"
                elif "Off-campus" not in locations:
                    study_mode = "On-Campus"
                else:
                    study_mode = "Hybrid"
                payload["study_mode"] = study_mode
                evidence.append(_ev(
                    "study_mode", study_mode,
                    "scrapy:page_data:offering.location", url,
                    f"offering locations: {sorted(locations)}", 0.75,
                ))
                on_campus = sorted(l for l in locations if l != "Off-campus")
                if on_campus:
                    payload["course_location"] = on_campus[0]
                    evidence.append(_ev(
                        "course_location", on_campus[0],
                        "scrapy:page_data:offering.location", url,
                        f"Offering campus: {on_campus[0]}", 0.75,
                    ))
        except Exception:
            pass

        yield {
            "name": name,
            "url": url,
            "payload": payload,
            "evidence": evidence,
        }

    def handle_error(self, failure):
        """Log page-data.json fetch failures; yield discovery-only item so the
        course is still staged via normal HTML extraction."""
        req = failure.request
        course_info = req.cb_kwargs.get("course_info", {})
        name = course_info.get("course_name") or req.url
        url  = course_info.get("course_website") or req.url
        self.logger.warning("MQ: page-data.json fetch failed for %s: %s", url, failure)
        yield {"name": name, "url": url}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ev(
    field_key: str,
    value: Any,
    method: str,
    source_url: str,
    snippet: str,
    confidence: float,
) -> dict:
    return {
        "field_key": field_key,
        "value": value,
        "normalized": value,
        "source_url": source_url,
        "page_type": "course",
        "method": method,
        "snippet": snippet,
        "confidence": confidence,
        "decision_status": "selected",
    }


def _parse_duration(raw: str) -> tuple[Optional[float], str]:
    """Parse '3 years', '18 months' etc. → (value, unit). Returns (None, '') on failure."""
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(year|years|month|months|semester|semesters)",
        (raw or "").lower(),
    )
    if not m:
        return None, ""
    val  = float(m.group(1))
    unit = m.group(2).rstrip("s")   # "year" / "month" / "semester"
    return val, unit


def _academic_level(study_level: str) -> Optional[str]:
    """Map Funnelback studyLevel string → canonical academic_level."""
    low = (study_level or "").lower()
    if "undergraduate" in low:
        return "Undergraduate"
    if "postgraduate" in low or "postgrad" in low:
        return "Postgraduate"
    if "research" in low or "doctorate" in low or "phd" in low:
        return "Doctorate"
    return None
