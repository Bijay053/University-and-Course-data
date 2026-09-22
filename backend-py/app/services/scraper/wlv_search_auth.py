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


class WlvCatalogueUnavailable(RuntimeError):
    """Safe, operator-facing failure rather than a misleading empty catalogue."""


class WlvAuthRecovery:
    """One refresh per discovery run, shared across pages and ordinary retries."""

    def __init__(self, endpoints: list[str], emit=None):
        self.enabled = endpoints == [WLV_ENDPOINT]
        self.refreshed = False
        self.emit = emit

    async def _status(self, message: str) -> None:
        if self.emit:
            try:
                await self.emit("status", message, phase="discover")
            except Exception:
                pass  # A disconnected progress stream must not abort recovery.

    async def get(self, client, url: str, *, params: dict, headers: dict):
        response = await client.get(url, params=params, headers=headers)
        if not self.enabled or url != WLV_ENDPOINT:
            return response
        if getattr(response, "status_code", None) not in (401, 403):
            return response
        if not self.refreshed:
            self.refreshed = True
            await self._status(
                "Wolverhampton changed its catalogue access. Refreshing it "
                "automatically and continuing this scrape."
            )
            token = await fetch_public_search_auth()
            headers["Authorization"] = f"Token {token}"
            response = await client.get(url, params=params, headers=headers)
            if response.status_code not in (401, 403):
                if response.status_code == 200:
                    await self._status("Wolverhampton catalogue access restored. Continuing discovery.")
                return response
        raise WlvCatalogueUnavailable(
            "Wolverhampton's catalogue is temporarily unavailable, even after "
            "automatic access recovery. No complete catalogue was obtained. "
            "Please retry the scrape later; no token or developer setup is needed."
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
        raise WlvCatalogueUnavailable(
            "Wolverhampton's search page is temporarily unavailable. "
            "Please retry the scrape later; access is refreshed automatically."
        ) from None
    token = parse_public_search_auth(html or "")
    if not token:
        raise WlvCatalogueUnavailable(
            "Wolverhampton's public catalogue access could not be verified. "
            "No complete catalogue was obtained. Please retry the scrape later; "
            "no token or developer setup is needed."
        )
    return token