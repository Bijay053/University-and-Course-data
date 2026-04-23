"""Course name extractor.

Best-effort: takes the first ``<h1>`` (then ``<title>``) and cleans it
the same way the Node ``preserveOriginalCapitalization`` helper does:
preserve standalone acronyms (MBA, BBA, ICT) and prepositions
(of/in/and/for) without lowercasing them. Strips trailing university or
campus suffixes ("- USQ", "| Charles Sturt University").
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.services.scraper.extractors.base import ExtractionResult

_PREPOSITIONS = {"of", "in", "and", "for", "the", "with", "to", "on", "by"}
_ACRONYMS = {
    "MBA", "BBA", "BA", "BS", "BSc", "MSc", "PhD", "ICT", "IT", "AI",
    "MD", "JD", "LLM", "LLB", "ME", "MEng", "EMBA", "GDip", "GCert",
    "MIT", "USQ", "CSU", "UTS", "ANU", "UNSW", "UoN", "RMIT",
}
_TITLE_SUFFIX = re.compile(
    r"\s*[\|\-–—:•]\s*(?:[A-Z][A-Za-z& ]{1,40}\s+(?:University|College|Institute|Academy)|USQ|CSU|UTS|ANU|UNSW|RMIT|MIT)\s*$",
    re.I,
)
_NON_COURSE_PREFIX = re.compile(
    r"^\s*(?:home|study|courses?|programs?)\s*[/>\\:|–-]\s*", re.I
)


def _smart_case(text: str) -> str:
    words = re.split(r"(\s+)", text.strip())
    out: list[str] = []
    for i, w in enumerate(words):
        if w.isspace() or not w:
            out.append(w)
            continue
        upper = w.upper().strip(",.;:()")
        bare = w.strip(",.;:()")
        if upper in _ACRONYMS:
            out.append(w.replace(bare, upper))
            continue
        lower = bare.lower()
        if i > 0 and lower in _PREPOSITIONS:
            out.append(w.replace(bare, lower))
            continue
        if bare and bare[0].isalpha():
            out.append(w.replace(bare, bare[0].upper() + bare[1:].lower()))
        else:
            out.append(w)
    return "".join(out)


def _clean(raw: str) -> str | None:
    if not raw:
        return None
    txt = re.sub(r"\s+", " ", raw).strip()
    txt = _NON_COURSE_PREFIX.sub("", txt)
    txt = _TITLE_SUFFIX.sub("", txt).strip(" -|·•")
    if not txt or len(txt) < 3 or len(txt) > 200:
        return None
    return _smart_case(txt)


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str, float]] = []
    h1 = soup.find("h1")
    if h1:
        candidates.append(("h1", h1.get_text(" ", strip=True), 0.9))
    title = soup.find("title")
    if title:
        candidates.append(("title", title.get_text(" ", strip=True), 0.6))

    for method, raw, conf in candidates:
        cleaned = _clean(raw)
        if cleaned:
            return [
                ExtractionResult(
                    field_key="course_name",
                    value=cleaned,
                    normalized={"course_name": cleaned},
                    confidence=conf,
                    method=f"course_name.{method}",
                    snippet=raw[:160],
                )
            ]
    return []
