"""Bind ARU's standard policy only through an explicit course-owned referral.

The URL or stored degree level alone is never applicability evidence. Professional,
research, foundation and conflicting courses need their own requirements.
"""
import asyncio
import re
import time
from urllib.parse import urljoin, urlparse
from weakref import WeakKeyDictionary

from bs4 import BeautifulSoup

SCHEMA = 1
POLICY_URL = "https://www.aru.ac.uk/international/entry-requirements"
FIELDS = tuple("ielts_" + field for field in
               ("overall", "listening", "reading", "writing", "speaking"))
_POLICIES = WeakKeyDictionary()
_EXCEPTION = re.compile(
    r"\b(?:research|PhD|MPhil|MRes|MArch|doctor(?:ate|al)?|DBA|foundation|pathway|"
    r"nurs\w*|midwif\w*|medicin\w*|medical|clinical|health\w*|psycholog\w*|"
    r"physiotherap\w*|pharmac\w*|paramedic\w*|optometr\w*|dent\w*|"
    r"biomed\w*|counsell\w*|psychotherap\w*|"
    r"therap\w*|rehabilitat\w*|surg\w*|physician\w*|physiolog\w*|"
    r"radiograph\w*|audiolog\w*|podiatr\w*|osteopath\w*|"
    r"nutrition\w*|dietetic\w*|perioperat\w*|operating\s+department|"
    r"ophthalm\w*|prosthe\w*|orthoti\w*|practice|"
    r"occupational\s+therapy|speech\s+and\s+language|social\s+work|"
    r"PGCE|teaching|teacher\w*|veterinar\w*)\b", re.I,
)
_AWARD = re.compile(
    r"\b(?:BSc|BA|BEng|MEng|MArch|MRes|MPharm|LLB|MBA|MSc|MA|LLM|PGDip|PGCert|"
    r"PhD|MPhil|DBA|MBChB|BOptom)\b", re.I,
)
_REFERRAL = re.compile(
    r"English language requirements for (undergraduate|postgraduate) courses", re.I,
)


def text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_course_url(url):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in {"aru.ac.uk", "www.aru.ac.uk"}
            and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and re.fullmatch(r"/study/(?:undergraduate|postgraduate|postgraduate-research)/[^/]+/?", parsed.path))


def _page(html, url):
    if not is_course_url(url):
        return None
    soup = BeautifulSoup(html or "", "html.parser")
    canonical = soup.find("link", rel="canonical", href=True)
    if canonical and urljoin(url, canonical["href"]).rstrip("/") != url.split("#", 1)[0].rstrip("/"):
        return None
    for node in soup.select("nav, header nav, footer, aside, script, style, form"):
        node.decompose()
    title = text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    heading = soup.find("h1")
    if not heading or not _AWARD.search(title):
        return None
    name = text(heading.get_text(" ", strip=True))
    if not name or name.casefold() not in title.casefold():
        return None
    return soup, title, name


def owned_sections(html, url):
    """Exclude foundation admissions alternatives and page navigation."""
    page = _page(html, url)
    if not page:
        return []
    soup, _, _ = page
    sections = []
    for anchor in soup.find_all("a", href=True):
        if (not anchor.find_parent("header")
                and _REFERRAL.fullmatch(text(anchor.get_text(" ", strip=True)))):
            container = anchor.find_parent(class_="rte")
            if container:
                sections.append(text(container.get_text(" ", strip=True)))
    for heading in soup.find_all(["h2", "h3", "h4", "h5"]):
        if heading.find_parent("header"):
            continue
        if text(heading.get_text(" ", strip=True)).casefold() != "english language requirements":
            continue
        parts = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) in {"h2", "h3", "h4", "h5"}:
                break
            parts.append(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
            if sum(map(len, parts)) > 4000:
                break
        sections.append(text(" ".join(parts)))
    return list(dict.fromkeys(section for section in sections if section))


def referral(html, url):
    page = _page(html, url)
    if not page:
        return None
    soup, title, name = page
    if _EXCEPTION.search(title):
        return None
    sections = owned_sections(html, url)
    if not sections or any(re.search(r"\bIELTS\b.{0,100}\d", section, re.I) for section in sections):
        return None
    found = []
    for anchor in soup.find_all("a", href=True):
        match = _REFERRAL.fullmatch(text(anchor.get_text(" ", strip=True)))
        container = anchor.find_parent(class_="rte")
        if (not match or not container or anchor.find_parent("header")
                or urljoin(url, anchor["href"]) != POLICY_URL):
            continue
        level = match.group(1).lower()
        if f"/study/{level}/" not in urlparse(url).path:
            continue
        quote = text(container.get_text(" ", strip=True))
        if "meet our" not in quote.casefold() or "IELTS requirements" not in quote:
            continue
        found.append({"level": level, "url": POLICY_URL, "quote": quote,
                      "title": title, "courseTitle": name})
    unique = {(item["level"], item["quote"]): item for item in found}
    return next(iter(unique.values())) if len(unique) == 1 else None


def policy_values(policy_text, level):
    """Read the exact named level and a complete uniform four-skill rule."""
    if level not in {"undergraduate", "postgraduate"}:
        return None
    pattern = (
        rf"\b{level}\s+courses\s+IELTS\s*\(\s*Academic\s*\)\s+"
        r"Overall\s+([4-9](?:\.[05])?)\s+with\s+([4-9](?:\.[05])?)\s+"
        r"in\s+each\s+of\s+the\s+four\s+language\s+skills\s*"
        r"\(\s*listening,\s*speaking,\s*reading\s+and\s+writing\s*\)"
    )
    matches = list(re.finditer(pattern, text(policy_text), re.I))
    if len(matches) != 1:
        return None
    match = matches[0]
    suffix = text(policy_text)[match.end():match.end() + 180]
    suffix = re.split(r"\b(?:TOEFL|Pearson|PTE|Postgraduate courses|Undergraduate courses)\b",
                      suffix, maxsplit=1, flags=re.I)[0]
    if re.search(r"\b(?:except|however|but)\b|\b(?:writing|speaking|reading|listening)\b"
                 r".{0,25}\b[4-9](?:\.[05])?\b", suffix, re.I):
        return None
    overall, floor = map(float, match.groups())
    if not 4 <= floor <= overall <= 9:
        return None
    return ({field: overall if field == "ielts_overall" else floor for field in FIELDS},
            match.group(0))


def _name_key(value):
    value = re.sub(r"\s*-\s*ARU\s*$", "", value or "", flags=re.I)
    value = re.sub(r"\b(?:degree|course)\b", "", value, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def comparison(row, course, sources):
    """Server-only preview; both independent sources must remain verifiable."""
    from app.services.scrape_assistant_tools import quote_in_source

    scope = course.get("_aruEnglishReferral")
    def has_quote(quote, source):
        # Cached _fullText may join non-adjacent excerpts. Never use those
        # synthetic boundaries as a contiguous quotation.
        blocks = [source.get("text") or ""]
        blocks.extend(item.get("text") or "" for item in source.get("excerpts", [])
                      if isinstance(item, dict))
        return any(quote_in_source(quote, {"text": block}) for block in blocks)
    if (not course.get("verified") or course.get("sourceType") != "html"
            or course.get("_aruEnglishScopeVersion") != SCHEMA or not isinstance(scope, dict)
            or not is_course_url(course.get("url", ""))
            or course["url"].rstrip("/") != str(row.get("url") or "").rstrip("/")
            or scope.get("url") != POLICY_URL
            or f"/study/{scope.get('level')}/" not in urlparse(course["url"]).path
            or _EXCEPTION.search(str(row.get("name") or ""))
            or _name_key(scope.get("title")) != _name_key(row.get("name"))
            or not has_quote(scope.get("quote", ""), course)):
        return []
    found = []
    for source in sources:
        if (source.get("url") != POLICY_URL or not source.get("verified")
                or source.get("sourceType") != "html"):
            continue
        matched = policy_values(source.get("text"), scope.get("level"))
        if matched and has_quote(matched[1], source):
            found.append((tuple(matched[0][field] for field in FIELDS), matched[1], POLICY_URL))
    return found


async def _policy(fetch):
    """Coalesce reads per event loop; retain no clients or completed tasks."""
    loop = asyncio.get_running_loop()
    held = _POLICIES.get(loop)
    if held and held["expires"] > time.monotonic() and held.get("task") is None:
        return held["body"]
    if not held or held.get("task") is None:
        held = {"task": None, "expires": 0, "body": ""}
        _POLICIES[loop] = held

        async def load():
            try:
                held["body"] = await asyncio.wait_for(fetch(POLICY_URL), 15) or ""
            except (Exception, asyncio.CancelledError):
                held["body"] = ""
                raise
            finally:
                held["expires"] = time.monotonic() + (300 if held["body"] else 30)
                held["task"] = None
            return held["body"]

        held["task"] = loop.create_task(load())
    return await asyncio.shield(held["task"])


async def recover(html, url, payload, evidence, fetch):
    scope = referral(html, url)
    if not scope or _EXCEPTION.search(str(payload.get("course_name") or "")):
        return False
    raw = await _policy(fetch)
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.select("nav, header, footer, aside, script, style"):
        node.decompose()
    matched = policy_values(soup.get_text(" ", strip=True), scope["level"])
    if not matched:
        return False
    values, quote = matched
    # Equal values still need actual selected source evidence for verification.
    evidence[:] = [item for item in evidence if item.get("field_key") not in FIELDS]
    for field, value in values.items():
        payload[field] = value
        evidence.append({"field_key": field, "value": value, "confidence": 0.95,
                         "method": "english.aru_linked_central:table",
                         "source_url": POLICY_URL, "snippet": quote})
    methods = dict(payload.get("extraction_method") or {})
    methods.update({field: "english.aru_linked_central:table" for field in FIELDS})
    payload["extraction_method"] = methods
    return True
