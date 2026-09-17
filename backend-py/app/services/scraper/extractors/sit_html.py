"""Southern Institute of Technology course-page compaction."""

from __future__ import annotations

from html import escape
import re
from urllib.parse import unquote, urljoin, urlparse

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


def course_name_from_html(html: str) -> str:
    """Return the programme identity displayed by the SIT course shell."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#courseName") or soup.find("h1")
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def current_course_campus_urls(
    html: str,
    course_url: str,
    *,
    max_urls: int = 4,
) -> list[str]:
    """Return only campus-detail links owned by the current SIT programme."""
    if not html or max_urls < 1:
        return []
    parsed_course = urlparse(course_url)
    expected_prefix = (
        unquote(parsed_course.path).rstrip("/").casefold() + "/campus/"
    )
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select('a[href*="/campus/"]'):
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        candidate = urljoin(course_url, href.strip())
        parsed = urlparse(candidate)
        if (
            (parsed.hostname or "").casefold()
            not in {"sit.ac.nz", "www.sit.ac.nz"}
            or not unquote(parsed.path).casefold().startswith(expected_prefix)
        ):
            continue
        if candidate not in urls:
            urls.append(candidate)
            if len(urls) >= max_urls:
                break
    return urls


def merge_current_course_panels(
    shell_html: str,
    campus_html_pages: list[str],
) -> str:
    """Merge verified campus panels into one bounded current-programme page."""
    course_name = course_name_from_html(shell_html)
    if not course_name:
        return shell_html

    panels: list[tuple[str, BeautifulSoup, object]] = []
    for html in campus_html_pages:
        if (
            not has_current_course_panel(html)
            or course_name_from_html(html).casefold() != course_name.casefold()
        ):
            continue
        soup = BeautifulSoup(html, "html.parser")
        summary = soup.select_one(".CourseInfo.CourseSummary")
        campus = summary.select_one("#currentCampusName") if summary else None
        campus_name = (
            " ".join(campus.get_text(" ", strip=True).split()) if campus else ""
        )
        if campus_name:
            panels.append((campus_name, soup, summary))
    if not panels:
        return shell_html

    panels.sort(
        key=lambda item: (
            any(token in item[0].casefold() for token in ("online", "distance")),
            item[0].casefold(),
        )
    )
    physical_panels = [
        item
        for item in panels
        if not any(
            token in item[0].casefold()
            for token in ("online", "distance")
        )
    ]
    panels = physical_panels or panels
    campus_names = list(
        dict.fromkeys(
            re.sub(r"\s*/\s*hyflex\b.*$", "", item[0], flags=re.I).strip()
            for item in panels
        )
    )
    primary_summary = panels[0][2]
    primary_key_info = primary_summary.select_one(".keyInfoPane .row.no-gutters")
    columns = primary_key_info.find_all("div", recursive=False)
    facts: list[tuple[str, str]] = []
    for index in range(0, len(columns) - 1, 2):
        label = " ".join(columns[index].get_text(" ", strip=True).split()).rstrip(":")
        value = " ".join(columns[index + 1].get_text(" ", strip=True).split())
        if label and value:
            facts.append((label, value))

    date_texts: list[str] = []
    criteria_texts: list[str] = []
    for _, _, summary in panels:
        date_panel = summary.select_one(".lightGrey_bg_1.mb-4")
        if date_panel:
            value = " ".join(date_panel.get_text(" ", strip=True).split())
            if value and value not in date_texts:
                date_texts.append(value)
        criteria = summary.select_one('[id^="headerApplicationCriteria_"]')
        if criteria:
            value = " ".join(criteria.get_text(" ", strip=True).split())
            if value and value not in criteria_texts:
                criteria_texts.append(value)

    shell_soup = BeautifulSoup(shell_html, "html.parser")
    title = shell_soup.title.get_text(" ", strip=True) if shell_soup.title else ""
    facts_html = "".join(
        f"<div>{escape(label)}:</div><div>{escape(value)}</div>"
        for label, value in facts
    )
    dates_html = (
        '<div class="lightGrey_bg_1 mb-4">'
        + escape(" ".join(date_texts))
        + "</div>"
        if date_texts
        else ""
    )
    criteria_html = (
        '<div id="headerApplicationCriteria_recovered">'
        + escape(" ".join(criteria_texts))
        + "</div>"
        if criteria_texts
        else ""
    )
    return (
        "<!doctype html><html><head><title>"
        + escape(title)
        + "</title></head><body><span id=\"courseName\">"
        + escape(course_name)
        + '</span><div class="CourseInfo CourseSummary">'
        + '<span id="currentCampusName">'
        + escape(", ".join(campus_names))
        + '</span><div class="keyInfoPane"><div class="row no-gutters">'
        + facts_html
        + "</div></div>"
        + dates_html
        + criteria_html
        + "</div></body></html>"
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
        if label.lower() == "duration":
            value = re.sub(
                r"(?<=\d)[-–—](?=(?:years?|months?|weeks?|semesters?|trimesters?)\b)",
                " ",
                value,
                flags=re.I,
            )
            value = re.sub(
                r"\s*[,;.]?\s*\bup to\b"
                r"(?=[^.;]*\bpart[- ]time\b)[^.;]*(?:[.;]|$)",
                "",
                value,
                flags=re.I,
            )
            value = re.sub(
                r"\bpart[- ]time study is also available\b.*$",
                "",
                value,
                flags=re.I,
            ).strip(" ,.;")
        if (
            label.lower() == "study modes"
            and re.search(r"\bonsite\b", value, re.I)
            and re.search(r"\bflexible distance\b", value, re.I)
        ):
            value = "Blended"
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