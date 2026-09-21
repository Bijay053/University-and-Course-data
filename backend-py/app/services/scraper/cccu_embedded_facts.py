"""Deterministic facts from Canterbury Christ Church's embedded Redux state."""
from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

_REDUX_MARKER = "window.REDUX_DATA = "
_CCCU_HOSTS = {"canterbury.ac.uk", "www.canterbury.ac.uk"}
_AMOUNT_RE = re.compile(r"£\s*([0-9][0-9,]*)")
_YEAR_RE = re.compile(r"\b(20\d{2})\s*/\s*\d{2}\b")


def _redux_state(page_html: str) -> dict[str, Any] | None:
    marker_at = page_html.find(_REDUX_MARKER)
    if marker_at < 0:
        return None
    start = marker_at + len(_REDUX_MARKER)
    end = page_html.find("</script>", start)
    if end < 0:
        return None
    raw = page_html[start:end].strip().rstrip(";")
    # REDUX_DATA is JSON except for two JavaScript values used in unrelated
    # routing/listing state.
    raw = re.sub(r"\bundefined\b", "null", raw)
    raw = re.sub(
        r'new Date\(("(?:[^"\\]|\\.)*")\)',
        r"\1",
        raw,
    )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _selected_entry_year(
    state: dict[str, Any], url: str
) -> dict[str, Any] | None:
    routing = state.get("routing")
    entry = routing.get("entry") if isinstance(routing, dict) else None
    years = entry.get("entryYears") if isinstance(entry, dict) else None
    if not isinstance(years, list):
        return None
    candidates = [item for item in years if isinstance(item, dict)]
    if not candidates:
        return None

    selected = (parse_qs(urlparse(url).query).get("year") or [""])[0]
    selected = selected.replace("-", " ").strip().lower()
    if selected:
        for item in candidates:
            title = str((item.get("entryYear") or {}).get("entryTitle") or "")
            if title.strip().lower() == selected:
                return item

    # The live page defaults to the first current entry year when the crawler's
    # canonical discovery URL has no query string.
    return candidates[0]


def _table_rows(fragment: str) -> list[list[str]]:
    soup = BeautifulSoup(html_lib.unescape(fragment), "html.parser")
    rows: list[list[str]] = []
    for row in soup.select("tr"):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        if cells:
            rows.append(cells)
    return rows


def _fee_fact(entry_year: dict[str, Any]) -> dict[str, Any] | None:
    fee = entry_year.get("courseFees")
    if not isinstance(fee, dict):
        return None
    rows = _table_rows(str(fee.get("feesTable") or ""))
    if not rows:
        return None

    international_col: int | None = None
    for row in rows:
        for index, cell in enumerate(row):
            if re.search(r"\b(overseas|international)\b", cell, re.I):
                international_col = index
                break
        if international_col is not None:
            break
    if international_col is None:
        return None

    choices: list[tuple[bool, float, str]] = []
    for row in rows:
        if len(row) <= international_col or not row:
            continue
        label = row[0]
        if not re.search(r"\bfull[- ]?time\b", label, re.I):
            continue
        amount_match = _AMOUNT_RE.search(row[international_col])
        if not amount_match:
            continue
        amount = float(amount_match.group(1).replace(",", ""))
        choices.append(("foundation" in label.lower(), amount, " | ".join(row)))
    if not choices:
        return None

    is_foundation_course = "foundation" in str(
        entry_year.get("entryTitle") or ""
    ).lower()
    preferred = (
        next((choice for choice in choices if choice[0]), choices[0])
        if is_foundation_course
        else next((choice for choice in choices if not choice[0]), choices[0])
    )
    year_text = " ".join(
        [
            str((entry_year.get("entryYear") or {}).get("entryTitle") or ""),
            str(fee.get("entryTitle") or ""),
        ]
    )
    year_match = _YEAR_RE.search(year_text)
    return {
        "international_fee": preferred[1],
        "currency": "GBP",
        "fee_term": "Annual",
        "fee_year": int(year_match.group(1)) if year_match else None,
        "snippet": preferred[2],
    }


def _english_fact(entry_year: dict[str, Any]) -> dict[str, float] | None:
    fragments: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif (
            isinstance(value, str)
            and "standard undergraduate" in value.lower()
            and "postgraduate" in value.lower()
            and "ielts" in value.lower()
        ):
            normalized = " ".join(
                BeautifulSoup(value, "html.parser")
                .get_text(" ", strip=True)
                .split()
            )
            if (
                "standard undergraduate and postgraduate" in normalized.lower()
                and "ielts" in normalized.lower()
            ):
                fragments.append(normalized)

    # The selected course embeds the University's international requirements
    # accordion. Restrict collection to that owned field, not related courses.
    collect(entry_year.get("internationalAside"))
    for fragment in fragments:
        text = fragment
        match = re.search(
            r"Standard undergraduate and postgraduate programmes.{0,180}?"
            r"([4-9](?:\.\d)?)\s+overall"
            r"(?:\s+with\s+no\s+element\s+below\s+([4-9](?:\.\d)?))?",
            text,
            re.I,
        )
        if not match:
            continue
        overall = float(match.group(1))
        result = {"ielts_overall": overall}
        if match.group(2):
            band = float(match.group(2))
            result.update(
                {
                    "ielts_listening": band,
                    "ielts_reading": band,
                    "ielts_writing": band,
                    "ielts_speaking": band,
                }
            )
        return result
    return None


def extract_cccu_embedded_facts(
    page_html: str, url: str
) -> dict[str, Any] | None:
    """Return selected-year fee and English facts for an official CCCU course."""
    parsed = urlparse(url)
    if (
        parsed.hostname not in _CCCU_HOSTS
        or not parsed.path.startswith("/study-here/courses/")
    ):
        return None
    state = _redux_state(page_html)
    if state is None:
        return None
    entry_year = _selected_entry_year(state, url)
    if entry_year is None:
        return None
    return {
        "fee": _fee_fact(entry_year),
        "english": _english_fact(entry_year),
    }