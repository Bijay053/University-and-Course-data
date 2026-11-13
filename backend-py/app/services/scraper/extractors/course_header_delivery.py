"""Delivery from a course-owned CMS hero, not site-wide support copy."""
import re

from bs4 import BeautifulSoup

from app.services.scraper.field_validation import invalid_location

_VIRTUAL = r"(?:online|distance(?:\s+learning)?|external|remote|virtual)"
_ONLINE_ONLY = re.compile(rf"(?:(?:100%|fully)\s+)?{_VIRTUAL}", re.I)
_NOISE = re.compile(
    r"\b(?:resources?|journals?|modules?|programmes?|courses?|support|apply|"
    r"register|information|materials?|study\s+locations)\b", re.I,
)


def _mode(value, *, location):
    value = " ".join(value.split())
    if not location:
        value = re.sub(r"\s*\((?:full[- ]time|part[- ]time)\)\s*$", "", value, flags=re.I)
    if _ONLINE_ONLY.fullmatch(value):
        return "Online"
    if re.fullmatch(r"on[- ]campus|in[- ]person|face[- ]to[- ]face", value, re.I):
        return "On Campus"
    if re.fullmatch(r"blended|hybrid|mixed[- ]mode", value, re.I):
        return "Blended"
    parts = re.split(r"\s+(?:and|or)\s+|[,/&]", value, flags=re.I)
    if len(parts) > 1:
        classified = [_mode(p.strip(), location=location) for p in parts if p.strip()]
        if classified and all(classified):
            unique = set(classified)
            return next(iter(unique)) if len(unique) == 1 else "Blended"
    if (location and value and value[0].isupper()
            and not re.search(rf"\b{_VIRTUAL}\b", value, re.I)
            and not _NOISE.search(value) and len(value.split()) <= 12
            and not invalid_location(value)):
        return "On Campus"
    return None


def course_header_delivery(html):
    """Return (mode, verbatim quote), None when absent, or (None, quote) on conflict.

    This component owns an H1 and its labelled facts. A footer address,
    university location picker or generic online-support matrix cannot supply
    this evidence. Explicit hero delivery labels outrank a physical address.
    """
    if "course-page-header-wrapper" not in (html or ""):
        return None
    soup = BeautifulSoup(html, "html.parser")
    for node in list(soup.select(
        "script,style,noscript,nav,footer,aside,form,select,[hidden],"
        "[aria-hidden='true'],[role='navigation'],[class*='related']"
    )):
        node.decompose()
    for node in list(soup.find_all(style=True)):
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", node["style"], re.I):
            node.decompose()
    headers = [h for h in soup.select(".course-page-header-wrapper") if h.find("h1")]
    if len(headers) != 1:
        return None
    header = headers[0]
    title = header.find("h1").get_text(" ", strip=True)
    if not title or any(h.get_text(" ", strip=True) != title for h in soup.find_all("h1")):
        return None
    deliveries, locations, ambiguous = [], [], []
    for label in header.find_all(["strong", "b", "dt"]):
        text = label.get_text(" ", strip=True)
        match = re.match(r"^(Location|Delivery(?: mode)?|Mode of study|Study mode)\s*:\s*(.*)$", text, re.I)
        if not match:
            continue
        if not match[2] and label.parent.name == "p":
            text = label.parent.get_text(" ", strip=True)
            match = re.match(r"^(Location|Delivery(?: mode)?|Mode of study|Study mode)\s*:\s*(.*)$", text, re.I)
        if not match:
            continue
        is_location = match[1].casefold() == "location"
        mode = _mode(match[2].strip(), location=is_location)
        if mode:
            (locations if is_location else deliveries).append((mode, text))
        elif not is_location and re.search(r"\bonline\b|\bcampus\b|\bblended\b|\bhybrid\b", match[2], re.I):
            ambiguous.append(text)
    if ambiguous:
        return None, ambiguous[0]
    facts = deliveries or locations
    if not facts:
        return None
    modes = {mode for mode, _ in facts}
    quote = facts[0][1]
    return (next(iter(modes)) if len(modes) == 1 else None), quote
