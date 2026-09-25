"""University of Law's course-owned fee tabs, without audience flattening.

Amounts are gross tuition, never bursary alternatives. A scalar is returned
only when every applicable campus publishes the same price.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

METHOD = "fee.ulaw_course_authority"


def validated_fee_variants(row) -> dict | None:
    """Validate persisted options against the exact current fee tuple and URL.

    Metadata alone is not permission to suppress missing/suspicious-fee checks.
    Editing any companion or detaching the source invalidates this recovery.
    """
    import math

    def get(key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

    url = get("course_website") or ""
    metadata = get("extraction_method")
    if not is_ulaw_course(url) or not isinstance(metadata, dict):
        return None
    if metadata.get("international_fee") not in {METHOD, METHOD + ":null"}:
        return None
    value = metadata.get("fee_variants")
    if not isinstance(value, dict) or value.get("status") not in {"uniform", "range"}:
        return None
    selected, options = value.get("selected"), value.get("options")
    if not isinstance(selected, list) or not selected or not isinstance(options, list):
        return None
    amounts = set()
    for option in selected:
        if not isinstance(option, dict) or option not in options:
            return None
        amount = option.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (float, int)) or not math.isfinite(amount) or amount <= 0:
            return None
        if (
            str(option.get("source_url", "")).rstrip("/") != url.rstrip("/")
            or option.get("currency") != "GBP"
            or option.get("year") != value.get("fee_year")
            or option.get("period") != value.get("fee_term")
            or option.get("period") not in {"Annual", "Full Course"}
            or not option.get("campus")
            or not option.get("study_variant")
            or not str(option.get("snippet", "")).startswith("International Students | ")
        ):
            return None
        amounts.add(amount)
    expected = next(iter(amounts)) if len(amounts) == 1 else None
    if value["status"] != ("uniform" if expected is not None else "range"):
        return None
    if value.get("international_fee") != expected or get("international_fee") != expected:
        return None
    if any(get(key) != value.get(key) for key in ("fee_year", "fee_term")):
        return None
    if (get("currency") or get("fee_currency")) != "GBP" or value.get("currency") != "GBP":
        return None
    return value
_YEAR = re.compile(r"\b(20\d{2})/\d{2}\s+Course Fees?", re.I)
_MONEY = re.compile(r"£\s*([\d,]+(?:\.\d{2})?)")
_INTL = re.compile(
    r"^(?:PG\s*(?:Dip|Cert)\s+)?(?:International\s+(?:Students|\(non-domestic\)\s*students)"
    r"|Non[-\s]domestic(?:\s+students)?)\b", re.I,
)
_VARIANT = re.compile(
    r"(professional practice|foundation year|postgraduate diploma|postgraduate certificate|"
    r"PG\s*Dip|PG\s*Cert|LLM Master of Laws?)", re.I
)
_CAMPUS = (
    r"Outside(?: of)? London|Non[- ]London|London(?: Bloomsbury| Moorgate| and Outside London)?"
    r"|Birmingham/Manchester|All locations|All campuses"
)
_DATES = re.compile(
    r"between\s+(\d{1,2}\s+\w+\s+20\d{2})\s*[-–]\s*(\d{1,2}\s+\w+\s+20\d{2})",
    re.I,
)


def _cohort_dates(text: str) -> tuple[date, date] | None:
    match = _DATES.search(text)
    if match:
        try:
            return tuple(datetime.strptime(value, "%d %B %Y").date() for value in match.groups())
        except ValueError:
            return None
    return None


def is_ulaw_course(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"law.ac.uk", "www.law.ac.uk"} and bool(
        re.fullmatch(r"/study/(undergraduate|postgraduate)/[^/]+/[^/]+/?", parsed.path)
    )


def parse_course_fees(html: str, url: str, *, today: date | None = None) -> dict | None:
    """Return exact options and the currently published cohort selection.

    The 2026-style template uses audience-labelled tabs; the older template
    uses international paragraphs/list headings inside its Fees tab. Neither
    path searches navigation, other courses, PDFs or institution-wide defaults.
    """
    if not is_ulaw_course(url):
        return None
    soup = BeautifulSoup(html, "html.parser")
    # Rich-text editors split digits across adjacent inline spans/strong tags
    # (e.g. £1<span>6,500</span>, <strong>2</strong><strong>027/28</strong>).
    # Preserve browser-visible adjacency before adding block-text separators.
    for inline in soup.find_all(["span", "strong", "em", "u", "b", "i"]):
        inline.unwrap()
    soup.smooth()
    title = soup.find("h1")
    title_text = title.get_text(" ", strip=True) if title else ""
    # A top-up is a complete award, not the annual price of a longer LLB.
    # Require the course's own introduction to explicitly describe one year.
    intro = " ".join(
        node.get_text(" ", strip=True) for node in soup.find_all(["p", "li"])
        if node.find_parent(id=["overview", "course-details-header-intro"])
        and "top up" in node.get_text(" ", strip=True).lower().replace("-", " ")
    )
    top_up_total = bool(
        re.search(r"top[\s-]*up", title_text, re.I)
        and re.search(r"(?:one|1)[-\s]+year", intro, re.I)
    )
    if re.search(r"top[\s-]*up", title_text, re.I) and not top_up_total:
        for heading in soup.find_all(["h2", "h3"]):
            if heading.get_text(" ", strip=True).lower() == "course structure":
                block = heading.find_parent(class_="accordionblock")
                years = re.findall(r"\bYear\s+(\d+)\b", block.get_text(" ", strip=True)) if block else []
                top_up_total = bool(years and set(years) == {"1"})
    course_intro = soup.find(id="course-details-header-intro")
    intro_text = course_intro.get_text(" ", strip=True) if course_intro else ""
    joint_awards = bool(re.search(
        r"Postgraduate Diploma and Postgraduate Certificate", intro_text, re.I,
    ))
    panels = []
    for a in soup.select('a[role="tab"][href^="#"]'):
        label = a.get_text(" ", strip=True)
        target = soup.find(id=a["href"][1:])
        if target and re.fullmatch(r"International Students", label, re.I):
            panels.append((target, True))
        elif target and re.fullmatch(r"(?:Course )?Fees(?: and Funding)?", label, re.I):
            panels.append((target, False))
    options = []
    for panel, international_panel in panels:
        nodes = [
            node for node in panel.find_all(["tr", "p", "li", "h3", "h4"])
            if node.name == "tr" or not node.find_parent("tr")
        ]
        # A year's own fee-reduction note explicitly addresses international
        # full-time students. This is not an institution-wide home-fee default.
        cohort_international_notes = {}
        note_year = None
        for node in nodes:
            text = node.get_text(" ", strip=True)
            ym = _YEAR.search(text)
            if ym:
                note_year = int(ym[1])
            elif note_year and re.search(
                rf"^Full.time international students\b.*fee reduction.*{note_year}/\d{{2}}",
                text, re.I,
            ):
                cohort_international_notes[note_year] = text
        year = None
        year_heading = ""
        variant = "Standard"
        audience = international_panel
        audience_label = "International Students"
        domestic_scope = False
        term = None
        # Paragraphs nested inside rows are read once, as part of that row.
        for node in nodes:
            text = node.get_text(" ", strip=True)
            ym = _YEAR.search(text)
            if ym:
                year = int(ym[1])
                year_heading = text
                variant = "Standard"
                audience = international_panel
                domestic_scope = False
                audience_label = "International Students"
                term = None
                continue
            inline_award = re.match(r"^(PG\s*(?:Dip|Cert))\s+(?:Non[-\s]domestic|Domestic)", text, re.I)
            if inline_award:
                variant = inline_award[1]
            elif not _MONEY.search(text) and _VARIANT.search(text):
                variant = text.rstrip(": ")
                continue
            if not international_panel:
                if _INTL.match(text):
                    audience = True
                    domestic_scope = False
                    audience_label = _INTL.match(text)[0]
                    term = "Annual" if re.search(r"per year|annual", text, re.I) else None
                elif re.match(r"(?:PG\s*(?:Dip|Cert)\s+)?(?:Domestic|UK|Home)\b", text, re.I):
                    audience = False
                    domestic_scope = True
                elif node.name == "p" and not _MONEY.search(text):
                    audience = False
            audience_note = ""
            if not audience and not domestic_scope:
                if re.search(r"\bor\s+£.*including a\s+£.*International Bursary", text, re.I):
                    audience_note = "Published gross course fee with an explicit International Bursary alternative"
                elif year in cohort_international_notes:
                    audience_note = cohort_international_notes[year]
            if not year or (not audience and not audience_note):
                continue
            cells = node.find_all(["td", "th"], recursive=False) if node.name == "tr" else []
            pieces = [(cells[0].get_text(" ", strip=True), cells[-1].get_text(" ", strip=True))] if len(cells) >= 2 else []
            if not cells:
                # br-separated paragraphs and li campus rows use an explicit
                # campus label. Stop at the next label, not at bursary amounts.
                for match in re.finditer(
                    rf"({_CAMPUS})\s*:\s*(.*?)(?=(?:{_CAMPUS})\s*:|$)",
                    text, re.I,
                ):
                    pieces.append((match[1], match[2]))
            for campus, price in pieces:
                money = _MONEY.match(price.strip())
                if not money or not re.fullmatch(
                    _CAMPUS,
                    campus, re.I,
                ):
                    continue
                amount = float(money[1].replace(",", ""))
                # Course Fees explicitly names the whole PG programme; UG
                # annual semantics must be explicitly published, not assumed.
                period = (
                    "Annual" if re.search(r"per year|per annum|annual", price, re.I)
                    else term or ("Full Course" if "/postgraduate/" in url or top_up_total else None)
                )
                snippet = f"International Students | {year_heading} | {variant} | {campus}: {price}"
                if audience_note:
                    snippet += f" | International applicability: {audience_note}"
                elif "non-domestic" in audience_label.lower():
                    snippet += f" | Published audience: {audience_label}"
                options.append({
                    "amount": amount, "currency": "GBP", "campus": campus,
                    "study_variant": variant, "year": year, "period": period,
                    "source_url": url,
                    "snippet": snippet,
                })
    if not options:
        return None
    # Prefer the current start-year, otherwise the next advertised year. Never
    # silently relabel historical-only published fees as a current fee.
    now = today or date.today()
    current = now.year
    years = sorted({o["year"] for o in options if o["year"] >= current})
    dated_current = {
        o["year"] for o in options
        if (bounds := _cohort_dates(o["snippet"])) and bounds[0] <= now <= bounds[1]
    }
    open_years = {
        o["year"] for o in options
        if "on or after" in o["snippet"].lower() and o["year"] <= current
    }
    selected_year = (
        max(dated_current) if dated_current else
        current if current in years else
        max(open_years) if open_years else
        years[0] if years else None
    )
    wants_practice = bool(re.search("professional practice", title_text, re.I))
    wants_foundation = bool(re.search("foundation", title_text, re.I))
    def applicable(o):
        v = o["study_variant"].lower()
        if re.search(r"postgraduate diploma|pg\s*dip", v):
            return joint_awards or bool(re.search(r"postgraduate diploma|pg\s*dip", title_text, re.I))
        if re.search(r"postgraduate certificate|pg\s*cert", v):
            return joint_awards or bool(re.search(r"postgraduate certificate|pg\s*cert", title_text, re.I))
        if "professional practice" in v:
            return wants_practice
        if "foundation" in v:
            return wants_foundation
        return not wants_practice and not wants_foundation
    selected = [o for o in options if o["year"] == selected_year and applicable(o)]
    amounts = {o["amount"] for o in selected}
    periods = {o["period"] for o in selected}
    scalar = next(iter(amounts)) if len(amounts) == 1 and len(periods) == 1 else None
    return {
        "international_fee": scalar, "currency": "GBP" if selected else None,
        "fee_year": selected_year if selected else None,
        "fee_term": next(iter(periods)) if len(periods) == 1 else None,
        "options": options, "selected": selected,
        "status": "uniform" if scalar is not None else ("range" if selected else "unresolved"),
    }


def apply_course_fee_authority(html: str, url: str, payload: dict, evidence: list) -> dict | None:
    result = parse_course_fees(html, url)
    if result is None:
        return None
    for key in ("international_fee", "currency", "fee_year", "fee_term"):
        payload[key] = result[key]
    # Replace competing broad-page evidence, including rejected domestic values.
    evidence[:] = [e for e in evidence if e.get("field_key") not in {
        "international_fee", "currency", "fee_year", "fee_term",
    }]
    import json
    evidence.append({
        "field_key": "international_fee", "value": result["international_fee"],
        "confidence": 0.99, "method": METHOD, "source_url": url,
        "snippet": json.dumps(result, ensure_ascii=False),
    })
    return result


async def recover_course_fee_only(url: str) -> dict | None:
    """Bounded live deterministic fee-only recovery; otherwise use normal pipeline.

    This optimization never returns partial/ambiguous authority and never
    changes unrelated fields. Network failure is not a fabricated extraction.
    """
    if not is_ulaw_course(url):
        return None
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
        if str(response.url).rstrip("/") != url.rstrip("/"):
            return None
    except httpx.HTTPError:
        return None
    payload = {"course_website": url}
    evidence = []
    result = apply_course_fee_authority(response.text, url, payload, evidence)
    if not result:
        return None
    payload["extraction_method"] = {"international_fee": METHOD, "fee_variants": result}
    if not validated_fee_variants(payload):
        return None
    return {"url": url, "payload": payload, "evidence": evidence}