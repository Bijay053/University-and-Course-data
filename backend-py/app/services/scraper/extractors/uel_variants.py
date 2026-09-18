"""UEL award-route identity and strictly route-owned extraction documents.

Intake tabs repeat routes; applicant and attendance rows are not separate
awards. Only distinct labelled award groups create separate course records.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag


_SELECTOR = "uel_variant"
_MONTHS = (
    "January February March April May June July August September October "
    "November December"
).split()


@dataclass(frozen=True)
class UELVariant:
    key: str
    name: str
    url: str
    html: str
    international: bool
    full_time: bool


def is_uel_course_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower() in {"uel.ac.uk", "www.uel.ac.uk"} and (
        parsed.path.lower().startswith(("/undergraduate/courses/", "/postgraduate/courses/"))
    )


def uel_variant_key(url: str) -> str:
    if not is_uel_course_url(url):
        return ""
    values = [v for k, v in parse_qsl(urlsplit(url).query) if k == _SELECTOR]
    if len(values) > 1:
        raise ValueError("Multiple UEL route selectors are not allowed")
    if values and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", values[0]):
        raise ValueError("Invalid UEL route selector")
    return values[0] if values else ""


def uel_source_url(url: str) -> str:
    if not is_uel_course_url(url):
        return url
    parsed = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                       if k != _SELECTOR])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), query, ""))


def _variant_url(url: str, key: str) -> str:
    parsed = urlsplit(uel_source_url(url))
    query = parse_qsl(parsed.query, keep_blank_values=True) + [(_SELECTOR, key)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")


def _route_name(title: str, label: str) -> str:
    if re.fullmatch(r"degree", label, re.I):
        return title
    if re.match(r"degree\s+with\b", label, re.I):
        suffix = re.sub(r"^degree\s+with\s+", "", label, flags=re.I)
        return f"{title} with {suffix}"
    # UEL repeats the base course title in MFA and placement rows. The group
    # heading, not the repeated row title, is the award authority.
    match = re.match(r"^(MA|MFA|MSc|MBA|MRes|MPhil|PhD|BSc|BA|BEng)\b", label, re.I)
    if match:
        title = re.sub(
            r"\s+(?:MA|MFA|MSc|MBA|MRes|MPhil|PhD|BSc|BA|BEng)"
            r"(?:\s+\(Hons\))?(?:\s+with\s+.*)?$", "", title, flags=re.I,
        )
        return f"{title} {label}"
    return f"{title} — {label}"


def _duration_text(raw: str, label: str) -> str:
    # A slash here is a pair of route lengths, not half a year. Select only
    # where the award heading supplies the missing standard/placement meaning.
    match = re.fullmatch(r"(\d+)\s*[/–-]\s*(\d+)\s*(years?|yrs?)", raw, re.I)
    if match:
        low, high = sorted((int(match[1]), int(match[2])))
        return f"{high if 'placement' in label.casefold() else low} years"
    return raw


def _tab_order(tab: Tag) -> tuple[int, int]:
    text = str(tab.get("aria-label") or tab.get("id") or "")
    year = re.search(r"20\d{2}", text)
    month = next((i for i, name in enumerate(_MONTHS, 1)
                  if name.casefold() in text.casefold()), 0)
    return (int(year[0]) if year else 0, month)


def _requirements(soup: BeautifulSoup, label: str) -> str:
    matches: list[str] = []
    # Undergraduate pages bind a shared entry dialog to a second labelled
    # chooser. These data-label/data-details-screen relationships are explicit
    # ownership, unlike assuming the first accordion means the standard degree.
    for button in soup.select("[data-details-screen][data-label]"):
        button_label = re.sub(
            r"\s*\(including contextual offer\)\s*$", "",
            str(button.get("data-label") or ""), flags=re.I,
        )
        if _slug(button_label) != _slug(label):
            continue
        owner = button.find_parent("dialog")
        screen = owner.find(id=button.get("data-details-screen")) if owner else None
        screen_label = re.sub(
            r"\s*\(including contextual offer\)\s*$", "",
            str(screen.get("data-label") or ""), flags=re.I,
        ) if screen else ""
        if screen is not None and _slug(screen_label) == _slug(label):
            matches.append(str(screen))
    for button in soup.select("[data-modal-id]"):
        text = str(button.get("aria-label") or "")
        prefix = "Full entry requirements for "
        if not text.casefold().startswith(prefix.casefold()):
            continue
        if _slug(text[len(prefix):]) != _slug(label):
            continue
        modal = soup.find(id=button.get("data-modal-id"))
        if modal is not None and not modal.select("[data-details-screen]"):
            # Closed dialogs are hidden in the source but their explicit
            # button binding proves the selected route owns this content.
            matches.append(str(modal))
    if not matches:
        return ""
    scoped = BeautifulSoup("".join(matches), "html.parser")
    for node in scoped.select("script, style, button, nav"):
        node.decompose()
    for node in scoped.select("dialog"):
        node.name = "section"
    for node in scoped.select("section, .entry-requirements-details-screen"):
        for attr in ("hidden", "aria-hidden", "style"):
            node.attrs.pop(attr, None)
    return str(scoped)


def _scoped_html(
    soup: BeautifulSoup, title: str, label: str,
    rows: list[tuple[Tag, Tag]], international: bool, full_time: bool,
) -> str:
    rows = sorted(rows, key=lambda item: _tab_order(item[0]), reverse=True)
    months: list[str] = []
    row_documents: list[str] = []
    duration = ""
    fee_text = ""
    for tab, original in rows:
        row = BeautifulSoup(str(original), "html.parser")
        length = row.select_one(".attendance-type-yr")
        mode = row.select_one(".attendance-type")
        raw_length = _text(length)
        if not raw_length and mode:
            raw_length = re.sub(r"^(?:full|part)[- ]?time\s*,?\s*", "", _text(mode), flags=re.I)
        normal_length = _duration_text(raw_length, label)
        if length is not None and normal_length:
            length.clear()
            length.append(normal_length)
        if normal_length and not duration:
            duration = normal_length
        fee = _text(row.select_one(".fee-type"))
        if fee and not fee_text:
            fee_text = fee
        tab_text = str(tab.get("aria-label") or tab.get("id") or "")
        for month in _MONTHS:
            if month.casefold() in tab_text.casefold() and month not in months:
                months.append(month)
        for node in row.select(".course-route"):
            node.clear()
            node.append(title)
        row_documents.append(str(row))
    months.sort(key=_MONTHS.index)
    facts = [("Study load", "Full Time" if full_time else "Part Time")]
    if duration:
        facts.append(("Duration", duration))
    if months:
        facts.append(("Intakes", ", ".join(months)))
    location = _text(soup.select_one(".course-location"))
    if location:
        facts.append(("Location", location))
    description = soup.select_one('meta[name="description"]')
    desc = str(description.get("content") or "") if description else ""
    facts_html = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts)
    requirements = _requirements(soup, label)
    # Keep annual tuition separate from supplementary placement/year-two fees.
    # The generic UK fee-table parser can use this unambiguous audience row.
    fee_html = ""
    amount = re.search(r"£\s*([\d,]+(?:\.\d{2})?)", fee_text)
    annual = bool(re.search(r"per\s*year|year\s*1\s*fees?", fee_text, re.I))
    if international and amount and annual:
        fee_html = (
            "<table><tr><th>Student</th><th>Study mode</th><th>Annual tuition fee (GBP)</th></tr>"
            f"<tr><td>International</td><td>{'Full-time' if full_time else 'Part-time'}</td>"
            f"<td>£{escape(amount[1])} per year</td></tr></table>"
        )
    return (
        f'<!doctype html><html><head><title>{escape(title)}</title></head><body>'
        f'<main data-uel-route="{escape(_slug(label))}"><h1>{escape(title)}</h1>'
        f"<p>{escape(desc)}</p><dl>{facts_html}</dl>{fee_html}"
        f"<section><h2>Course options — {escape(label)}</h2>{''.join(row_documents)}</section>"
        f'<section data-uel-requirements="{"matched" if requirements else "missing"}">'
        f"<h2>Entry requirements</h2>{requirements}</section>"
        f"{'<p>Domestic applicants only</p>' if not international else ''}"
        "</main></body></html>"
    )


def parse_uel_variants(html: str, url: str) -> list[UELVariant]:
    """Group awards across intake tabs; never group by applicant or study mode."""
    if not html or not is_uel_course_url(url):
        return []
    soup = BeautifulSoup(html, "html.parser")
    selected = uel_variant_key(url)
    labels = {_slug(_text(node)) for node in soup.select(
        ".course-options-content-div .degree-type"
    ) if _text(node)}
    modal_labels = {
        _slug(str(button.get("aria-label"))[len("Full entry requirements for "):])
        for button in soup.select("[data-modal-id][aria-label]")
        if str(button.get("aria-label")).casefold().startswith("full entry requirements for ")
    }
    multiple = len(labels) > 1 or len(modal_labels) > 1
    title = _text(soup.select_one("h1"))
    if not title:
        if multiple or selected:
            raise ValueError("UEL course routes have no authoritative course title")
        return []
    groups: dict[str, tuple[str, list[tuple[Tag, Tag]]]] = {}
    for tab in soup.select(".course-options-content-div"):
        for details in tab.select(".course-option-details"):
            # The label is a preceding sibling within this exact intake tab.
            header = details.find_previous_sibling()
            heading = header.select_one(".degree-type") if header else None
            if heading is None:
                if multiple or selected:
                    raise ValueError("UEL course option has no associated award heading")
                continue
            label = _text(heading)
            key = _slug(label)
            if not key:
                continue
            rows = details.select(".course-option-details-item-wrapper")
            if not rows:
                rows = details.select(".course-option-details__list-item")
            if not rows:
                if multiple or selected:
                    raise ValueError(f"UEL route {label!r} has no recognized option rows")
                continue
            groups.setdefault(key, (label, []))[1].extend((tab, row) for row in rows)
    if multiple and (len(groups) < 2 or labels - set(groups)):
        raise ValueError("UEL multi-award page has incomplete route evidence")
    if len(groups) < 2 and not selected:
        return []
    variants = []
    for key, (label, all_rows) in groups.items():
        for _, row in all_rows:
            audience = _text(row.select_one(".application-type"))
            attendance = _text(row.select_one(".attendance-type"))
            if not re.search(r"\b(?:international|home) applicant\b", audience, re.I):
                raise ValueError(f"UEL route {label!r} has unknown applicant eligibility")
            if not re.search(r"\b(?:full|part)[- ]?time\b", attendance, re.I):
                raise ValueError(f"UEL route {label!r} has unknown attendance")
        intl = [(tab, row) for tab, row in all_rows
                if re.search(r"\binternational applicant\b", _text(row.select_one(".application-type")), re.I)]
        eligible = intl or all_rows
        fulltime = [(tab, row) for tab, row in eligible
                    if re.search(r"\bfull[- ]?time\b", _text(row.select_one(".attendance-type")), re.I)]
        eligible = fulltime or eligible
        name = _route_name(title, label)
        variants.append(UELVariant(
            key=key, name=name, url=_variant_url(url, key),
            html=_scoped_html(soup, name, label, eligible, bool(intl), bool(fulltime)),
            international=bool(intl), full_time=bool(fulltime),
        ))
    return variants


def scope_uel_variant(html: str, url: str) -> str:
    key = uel_variant_key(url)
    if not key:
        return html
    for variant in parse_uel_variants(html, url):
        if variant.key == key:
            return variant.html
    raise ValueError(f"UEL variant {key!r} is not present in the source page")


def uel_requirement_bands(html: str) -> dict[str, float]:
    """Read explicit grouped skill scores only from the selected IELTS panel.

    "6.0 in writing and speaking, and 5.5 in listening and reading" is not an
    all-band floor. Keep each numeric clause separate; never infer a skill that
    the clause did not name.
    """
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one('[data-uel-requirements="matched"]')
    if panel is None:
        return {}
    scores: dict[str, float] = {}
    for paragraph in panel.select("p, li"):
        text = _text(paragraph)
        if not re.search(r"\bIELTS\b", text, re.I):
            continue
        for match in re.finditer(
            r"(?<![\d.])([0-9](?:\.[05])?)(?![\d.])\s+in\s+"
            r"((?:writing|speaking|listening|reading)\b[^.;\d]*)",
            text, re.I,
        ):
            score = float(match[1])
            if not 0 < score <= 9:
                continue
            for skill in re.findall(r"\b(writing|speaking|listening|reading)\b", match[2], re.I):
                field = f"ielts_{skill.casefold()}"
                if field in scores and scores[field] != score:
                    raise ValueError("UEL route has conflicting IELTS component requirements")
                scores[field] = score
    return scores