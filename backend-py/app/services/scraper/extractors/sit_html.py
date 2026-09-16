"""Southern Institute of Technology course-page compaction."""

from __future__ import annotations

from html import escape
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def is_sit_course_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        host in {"sit.ac.nz", "www.sit.ac.nz"}
        and parsed.path.lower().startswith("/programme/course/")
    )


def has_current_course_panel(html: str) -> bool:
    """Return whether SIT rendered the current programme's usable fact panel."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one(".CourseInfo.CourseSummary")
    return bool(
        summary
        and summary.select_one("#currentCampusName")
        and summary.select_one(".keyInfoPane .row.no-gutters")
    )


def compact_course_html(html: str) -> str:
    """Keep only SIT's current programme heading and course-owned summary."""
    if not html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    course_name = soup.select_one("#courseName") or soup.find("h1")
    summary = soup.select_one(".CourseInfo.CourseSummary")
    if course_name is None or summary is None:
        return _minimal_safe_page(soup, course_name)

    campus = summary.select_one("#currentCampusName")
    key_info = summary.select_one(".keyInfoPane .row.no-gutters")
    if campus is None or key_info is None:
        return _minimal_safe_page(soup, course_name)

    columns = key_info.find_all("div", recursive=False)
    facts: list[tuple[str, str]] = [
        ("Location", campus.get_text(" ", strip=True))
    ]
    for index in range(0, len(columns) - 1, 2):
        label = " ".join(columns[index].get_text(" ", strip=True).split()).rstrip(":")
        value = " ".join(columns[index + 1].get_text(" ", strip=True).split())
        if label and value:
            facts.append((label, value))

    intake_months: list[str] = []
    date_and_fee_panel = summary.select_one(".lightGrey_bg_1.mb-4")
    if date_and_fee_panel is not None:
        panel_text = " ".join(
            date_and_fee_panel.get_text(" ", strip=True).split()
        )
        for match in re.finditer(
            r"\b(?:Semester|Intake)\s+\d+\s*:\s*\d{1,2}\s+"
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\b",
            panel_text,
            re.I,
        ):
            month = match.group(1).title()
            if month not in intake_months:
                intake_months.append(month)

    application = summary.select_one('[id^="headerApplicationCriteria_"]')
    head_parts = (
        [
            str(node)
            for node in soup.head.find_all(["title", "meta"], recursive=True)
        ]
        if soup.head
        else []
    )
    facts_html = "<dl>" + "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>"
        for label, value in facts
    ) + "</dl>"
    dates_html = (
        f"<dl><dt>Intakes</dt><dd>{escape(', '.join(intake_months))}</dd></dl>"
        if intake_months
        else ""
    )
    application_html = str(application) if application is not None else ""
    return (
        "<!doctype html><html><head>"
        + "".join(head_parts)
        + "</head><body><h1>"
        + escape(course_name.get_text(" ", strip=True))
        + "</h1>"
        + facts_html
        + dates_html
        + application_html
        + "</body></html>"
    )


def _minimal_safe_page(soup: BeautifulSoup, course_name) -> str:
    """Return bounded evidence when SIT's expected course component drifts."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = course_name or soup.find("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else title
    return (
        "<!doctype html><html><head><title>"
        + escape(title)
        + "</title></head><body><h1>"
        + escape(heading_text)
        + "</h1></body></html>"
    )