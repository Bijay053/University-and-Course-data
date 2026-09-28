"""Validate BCU course-facts locations without accepting page-wide text."""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

BCU_KEYFACT_LOCATIONS = frozenset({
    "alexander stadium",
    "birmingham",
    "bournville",
    "city centre",
    "city south",
    "margaret street",
    "rbc digbeth",
    "royal birmingham conservatoire",
    "school of jewellery",
    "steamhouse",
    "wuhan textile university / bcu",
    "online",
    "distance learning",
    "uk campus",
})


def is_bcu_keyfact_location(value: str) -> bool:
    """Accept exact panel sites or combinations, but no partial-name matches."""
    normalized = value.strip().casefold()
    if normalized in BCU_KEYFACT_LOCATIONS:
        return True
    parts = [part.strip() for part in re.split(r"\s*(?:,| / )\s*", normalized)]
    return len(parts) > 1 and all(part in BCU_KEYFACT_LOCATIONS for part in parts)


# Both BCU specifications linked from this apprenticeship's own course page
# explicitly give City Centre as Location(s) of Study:
# Quantity Surveying: fafa2631-9a67-f011-8dca-6045bd0abbe1
# Real Estate: b26ec895-9a67-f011-8dca-6045bd0abbe1.
# Do not apply this to other apprenticeships or infer from shared facilities.
_SURVEYOR_SPEC_IDS = frozenset({
    "fafa2631-9a67-f011-8dca-6045bd0abbe1",
    "b26ec895-9a67-f011-8dca-6045bd0abbe1",
})


def bcu_course_specific_location(soup: BeautifulSoup, url: str) -> str | None:
    """Read only two verified course-owned sources missing a key-facts Location."""
    parsed = urlparse(url)
    if parsed.hostname not in {"bcu.ac.uk", "www.bcu.ac.uk"}:
        return None
    path = parsed.path.rstrip("/").lower()
    if path == "/courses/certificate-of-professionalism-in-innovation":
        # The Schedule panel explicitly says "Location: STEAMhouse,
        # Belmont Row, Birmingham, B4 7RQ". Require that exact address so
        # unrelated footer/map mentions cannot become a course location.
        for panel in soup.select("div.panel__inner"):
            heading = panel.find(["h2", "h3"])
            if not heading or heading.get_text(" ", strip=True).casefold() != "schedule":
                continue
            for paragraph in panel.find_all("p", recursive=False):
                if re.fullmatch(
                    r"Location:\s*STEAMhouse,\s*Belmont Row,\s*Birmingham,\s*B4 7RQ\.?",
                    paragraph.get_text(" ", strip=True),
                    re.I,
                ):
                    return "STEAMhouse"
    if re.fullmatch(
        r"/courses/chartered-surveyor-apprenticeship-bsc-hons-20\d{2}-\d{2}",
        path,
    ):
        linked_specs = set()
        for a in soup.select("a[href]"):
            if "course specification" not in a.get_text(" ", strip=True).casefold():
                continue
            spec = urlparse(urljoin(url, a["href"]))
            if spec.hostname in {"bcu.ac.uk", "www.bcu.ac.uk"} and spec.path.lower().startswith("/download/asset/"):
                linked_specs.add(spec.path.rsplit("/", 1)[-1].lower())
        if _SURVEYOR_SPEC_IDS <= linked_specs:
            return "City Centre"
    return None