"""Build the ``[course-page extracted fields] …`` footer that the
snapshot-builder uses to attribute fields back to the course_page
even when the rendered HTML body is sparse (CSU-style JS-hydrated
pages). Mirrors Node ``buildCoursePageProvenanceFooter``.
"""
from __future__ import annotations

from typing import Any, Mapping

_FOOTER_PREFIX = "\n\n[course-page extracted fields] "


def build_course_page_provenance_footer(data: Mapping[str, Any]) -> str:
    g = data.get
    parts: list[str] = []
    if g("course_name"):
        parts.append(f"courseName: {g('course_name')}")
    if g("degree_level"):
        parts.append(f"degreeLevel: {g('degree_level')}")
    if g("duration") is not None and g("duration_term"):
        parts.append(f"duration: {g('duration')} {g('duration_term')}")
    if g("study_mode"):
        parts.append(f"study mode: {g('study_mode')}")
    if g("course_location"):
        parts.append(f"location: {g('course_location')}")
    if g("international_fee") is not None:
        currency = g("currency") or "AUD"
        fee_term = f" {g('fee_term')}" if g("fee_term") else ""
        parts.append(f"international fee: {currency} {g('international_fee')}{fee_term}")
    intake_months = g("intake_months") or []
    if intake_months:
        parts.append(f"intake: {', '.join(intake_months)}")
    if g("ielts_overall") is not None:
        parts.append(f"IELTS {g('ielts_overall')}")
    if g("pte_overall") is not None:
        parts.append(f"PTE {g('pte_overall')}")
    if g("toefl_overall") is not None:
        parts.append(f"TOEFL {g('toefl_overall')}")
    if g("cambridge_overall") is not None:
        parts.append(f"Cambridge {g('cambridge_overall')}")
    if g("duolingo_overall") is not None:
        parts.append(f"Duolingo {g('duolingo_overall')}")
    if not parts:
        return ""
    return f"{_FOOTER_PREFIX}{'; '.join(parts)}."
