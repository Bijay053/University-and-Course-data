"""Flinders course-page compaction.

Flinders serves roughly 750 KB of AEM chrome per course.  The authoritative
course facts are in the title/hero and ``courses-fast-facts-v2`` component.
Keeping only those components prevents every generic extractor from repeatedly
parsing navigation, related-course and footer markup.
"""

from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup


def is_flinders_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "flinders.edu.au" or host.endswith(".flinders.edu.au")


def compact_course_html(html: str) -> str:
    if not html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    facts = soup.select_one(".courses-fast-facts-v2")
    if h1 is None or facts is None:
        return html

    hero = h1
    for parent in h1.parents:
        hero = parent
        if "section" in (parent.get("class") or []):
            break

    head_parts = [
        str(node)
        for node in soup.head.find_all(["title", "meta"], recursive=True)
    ] if soup.head else []
    return (
        "<!doctype html><html><head>"
        + "".join(head_parts)
        + "</head><body>"
        + str(hero)
        + str(facts)
        + "</body></html>"
    )