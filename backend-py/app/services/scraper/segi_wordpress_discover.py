"""Current SEGi Colleges course discovery through the protected WordPress API."""
from __future__ import annotations

import inspect
import json
import re
from html import unescape
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from app.services.scraper.http_fetcher import fetch_html_scrape_do

_ENDPOINT = "https://www.segi.edu.my/wp-json/wp/v2/search"
_PAGE_SIZE = 100
_MAX_PAGES = 5
_COURSE_TITLE_RE = re.compile(
    r"\b(?:"
    r"bachelor|master|doctor|phd|diploma|certificate|foundation|degree|"
    r"postgraduate|undergraduate|acca|a[ -]?level|pre-university|"
    r"ba|bsc|llb|mba|msc|med|meng|bed|beng|bba|dba"
    r")\b",
    re.I,
)


def _unwrap_rendered_json(body: str) -> Any:
    soup = BeautifulSoup(body, "html.parser")
    raw = soup.pre.get_text() if soup.pre else body
    return json.loads(unescape(raw))


def _is_course_result(item: dict[str, Any]) -> bool:
    if item.get("subtype") != "page":
        return False
    parsed = urlparse(str(item.get("url") or ""))
    title = BeautifulSoup(
        unescape(str(item.get("title") or "")),
        "html.parser",
    ).get_text(" ", strip=True)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.segi.edu.my"
        and bool(re.fullmatch(r"/[^/]+/", parsed.path))
        and bool(_COURSE_TITLE_RE.search(title))
    )


async def discover_segi_wordpress_courses(
    emit: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Return official pages whose indexed content contains ``Programme ID``."""

    async def _emit(message: str) -> None:
        if not emit:
            return
        try:
            result = emit(message)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        query = urlencode({
            "subtype": "page",
            "search": "Programme ID",
            "per_page": _PAGE_SIZE,
            "page": page,
        })
        body = await fetch_html_scrape_do(
            f"{_ENDPOINT}?{query}",
            render=True,
            max_retries=0,
        )
        if not body:
            if page == 1:
                await _emit("[DISCOVER] SEGi Colleges API unavailable")
            break
        try:
            items = _unwrap_rendered_json(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            break
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict) or not _is_course_result(item):
                continue
            url = str(item["url"])
            if url in seen:
                continue
            seen.add(url)
            title = BeautifulSoup(
                unescape(str(item.get("title") or "")),
                "html.parser",
            ).get_text(" ", strip=True)
            links.append({"name": re.sub(r"\s+", " ", title), "url": url})
        await _emit(
            f"[DISCOVER] SEGi Colleges API page {page}: "
            f"{len(links)} unique course pages"
        )
        if len(items) < _PAGE_SIZE:
            break
    return links