"""Resolve WLV's public browser-search credential without persisting it."""
from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup

WLV_SEARCH_PAGE = "https://www.wlv.ac.uk/search/"
WLV_ENDPOINT = (
    "https://searchcloud-1-eu-west-2.searchstax.com/"
    "29847/wolverhamptondevelopment-3254/emselect"
)


def parse_public_search_auth(html: str) -> str | None:
    """Accept only the credential paired with the exact known search endpoint."""
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        body = script.string or script.get_text()
        for config in re.finditer(r"\bconst\s+config\s*=\s*\{([^{}]+)\}", body, re.S):
            source = config.group(1)
            endpoint = re.search(r'\bsearchURL\s*:\s*["\']([^"\']+)["\']', source)
            token = re.search(r'\bsearchAuth\s*:\s*["\']([A-Za-z0-9_-]{16,256})["\']', source)
            if endpoint and endpoint.group(1) == WLV_ENDPOINT and token:
                return token.group(1)
    return None


async def fetch_public_search_auth() -> str:
    # Fixed official URL, not a user-controlled fetch destination. WLV blocks
    # datacentre requests, but its public search configuration is static HTML.
    from app.services.scraper.http_fetcher import fetch_html_scrape_do

    try:
        html = await asyncio.wait_for(
            fetch_html_scrape_do(
                WLV_SEARCH_PAGE, render=False, max_retries=0,
                request_timeout_seconds=40,
            ),
            timeout=45,
        )
    except Exception:
        raise RuntimeError("WLV public search authentication page could not be fetched") from None
    token = parse_public_search_auth(html or "")
    if not token:
        raise RuntimeError("WLV public search authentication could not be verified")
    return token