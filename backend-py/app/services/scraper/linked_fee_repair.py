"""Evidence-bound course fee links shared by repair validation and extraction."""
from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

LABELS = ("course fees", "programme fees", "program fees", "tuition fees",
          "international tuition fees")


def course_fee_links(html: str, url: str, config=None) -> list[str]:
    from .ai_repair_live import inspect_page, official_url
    page = inspect_page(url, html, config)
    if page.get("classification") != "course":
        return []
    soup = BeautifulSoup(page.get("owned_html") or "", "html.parser")
    result = []
    for node in soup.select("a[href]"):
        label = " ".join(node.get_text(" ", strip=True).casefold().split())
        if label not in LABELS or node["href"].startswith("#"):
            continue
        target = urldefrag(urljoin(url, node["href"]))[0]
        # An automatically learned rule never grants new host access.
        if target != urldefrag(url)[0] and official_url(target, url) and target not in result:
            result.append(target)
    return result[:2]


def tuition_records(html: str) -> list[dict]:
    """Read the intersection of explicit international and full-time labels."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main, [role=main], article")
    if main is None:
        return []
    for node in main.select("nav, aside, footer, header"):
        node.decompose()
    context = main.get_text(" ", strip=True)
    if not re.search(r"\b(?:course|tuition|programme|program)\s+fees?\b", context, re.I):
        return []
    if not re.search(r"each\s+year|single\s+academic\s+year|one\s+year\s+of\s+study|per\s+annum|annual\s+(?:tuition\s+)?fees", context, re.I):
        return []
    candidates = []
    for table in main.select("table"):
        text = table.get_text(" ", strip=True)
        if not re.search(r"\binternational\b", text, re.I):
            continue
        if not re.search(r"\bfull[\s-]*time\b", text, re.I):
            continue
        heading = table.select_one("caption") or table.find("tr")
        if heading is None:
            continue
        year_match = re.search(r"\b(20\d{2})\s*[/–-]\s*(?:20)?\d{2}\b",
                               heading.get_text(" ", strip=True))
        if not year_match:
            heading = table.find_previous(["h2", "h3", "h4", "h5"])
            if heading is None or heading not in main.descendants:
                continue
            year_match = re.search(r"\b(20\d{2})\s*[/–-]\s*(?:20)?\d{2}\b",
                                   heading.get_text(" ", strip=True))
            if not year_match:
                continue
        year = int(year_match[1])
        rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
                for row in table.select("tr")]
        if not rows:
            continue
        headers = rows[0]
        def column(pattern):
            return next((i for i, label in enumerate(headers)
                         if re.fullmatch(pattern, label, re.I)), None)
        intl = column(r"international(?: students?)?")
        fulltime = column(r"full[\s-]*time")
        home = column(r"UK|Home(?: students?)?")
        for row in rows[1:]:
            if len(row) != len(headers) or not row:
                continue
            if re.search(r"thesis|pending|placement|abroad|writing.up|bench|additional|retake", row[0], re.I):
                continue
            cell = None
            domestic = None
            if intl is not None and re.fullmatch(r"full[\s-]*time", row[0], re.I):
                cell = row[intl]
                domestic = row[home] if home is not None else None
            elif fulltime is not None and re.fullmatch(r"international(?: students?)?", row[0], re.I):
                cell = row[fulltime]
            if cell is None:
                continue
            if re.search(r"\btotal\b|full[\s-]*course|entire\s+(?:course|programme|program)", cell, re.I):
                continue
            def money(value):
                matches = re.findall(r"£\s*(\d[\d,]*(?:\.\d{2})?)", value or "")
                return float(matches[0].replace(",", "")) if len(matches) == 1 else None
            amount = money(cell)
            if amount is not None and not 1000 <= amount <= 250000:
                continue
            candidates.append({
                "international_fee": amount, "currency": "GBP",
                "fee_term": "Annual", "fee_year": year,
                "snippet": f"{year}: {' | '.join(headers)}; {' | '.join(row)}",
                "domestic_amount": money(domestic),
                "unpublished": bool(re.fullmatch(
                    r"£?\s*(TBC|TBA|to be confirmed|not yet published)"
                    r"(?:\s+per\s+(?:level|year))?\s*[*†]*", cell, re.I,
                )),
            })
    return candidates


async def published_fee(html: str, url: str) -> dict | None:
    candidates = tuition_records(html)
    if not candidates:
        return None
    latest = max(item["fee_year"] for item in candidates)
    candidates = [item for item in candidates if item["fee_year"] == latest]
    if any(item["international_fee"] is None for item in candidates):
        return None
    identities = {(item["international_fee"], item["currency"]) for item in candidates}
    if len(identities) != 1:
        return None
    return {key: value for key, value in {**candidates[0], "source_url": url}.items()
            if key not in {"domestic_amount", "unpublished"}}


def borrowed_domestic_fee(html: str, amount) -> dict | None:
    records = tuition_records(html)
    if not records or amount is None:
        return None
    latest = max(row["fee_year"] for row in records)
    return next((row for row in records
                 if row["fee_year"] == latest and row["unpublished"]
                 and row["domestic_amount"] == amount), None)


async def recover_fee(html: str, url: str, config, fetch) -> dict | None:
    """Fetch only current-course-owned official links; reject conflicting sources."""
    values = []
    for target in course_fee_links(html, url, config):
        source = await fetch(target)
        value = await published_fee(source, target) if source else None
        if value:
            values.append(value)
    keys = {(v["international_fee"], v["currency"], v["fee_year"]) for v in values}
    return values[0] if len(keys) == 1 else None
