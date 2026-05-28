"""Curtin University HTMX session priming.

Curtin's course pages render different content (and therefore different
fees, IELTS bands, intake months, durations) depending on a server-side
region cookie set by their HTMX segment-toggle widget. The widget POSTs
to ``/htmx/segmentToggle`` with a ``region=int|dom`` form body, but the
only durable side-effect of that POST is a ``user_region`` cookie. Once
``user_region=int`` is on the request, the same static URL renders the
international view (real ~$30k-$50k AUD tuition fees) instead of the
default domestic view (Commonwealth-Supported Place placeholder
amounts: ~$8k-$17k AUD).

A query-string rewrite (``?regionToggle=1``) does NOT work — verified
live: every value of that param returns identical bytes because the
HTMX server only honours the cookie. The previous Curtin per-uni YAML
declared an ``extraction.url_rewrites`` entry for ``regionToggle=1``
that was a silent no-op; cleaning that dead config up is intentionally
left for a separate commit so a regression here is easy to bisect.

This module exposes two helpers that the two Curtin-touching fetch
layers call on every per-course request:

* :func:`cookies_for_url` — dict shape, for ``httpx.AsyncClient.get``.
* :func:`playwright_cookies_for_url` — list-of-dicts shape, for
  ``BrowserContext.add_cookies``.

For non-Curtin hosts both return empty containers, so the helpers are
a true no-op for every other university in the fleet.

Host gate uses strict ``urlparse().hostname`` netloc matching against
the apex (or any ``*.curtin.edu.au`` subdomain) so substring URLs like
``?ref=www.curtin.edu.au`` from other universities cannot trigger the
side-effect.

Verified live (2026-05-12) on Bachelor of Science
(``/study/offering/course-ug-bachelor-of-science-science--b-scnce/``):

* Without cookie: static HTML fees = only $10,000 (CSP placeholder).
* With ``user_region=int`` cookie: static HTML fees = $44,222,
  $47,316, $132,666, $141,948 — Curtin's published international
  tuition (annual + whole-of-course).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

_APEX = "curtin.edu.au"
_COOKIE_NAME = "user_region"
_COOKIE_VALUE = "int"


def _host_matches(url: str) -> bool:
    """Strict apex-or-subdomain match on ``curtin.edu.au``."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return host == _APEX or host.endswith("." + _APEX)


def cookies_for_url(url: str) -> dict[str, str]:
    """Cookies to attach to an ``httpx`` request for ``url``.

    Returns an empty dict for non-Curtin hosts so callers can pass the
    result directly to ``httpx.AsyncClient.get(..., cookies=...)``.
    """
    if not _host_matches(url):
        return {}
    return {_COOKIE_NAME: _COOKIE_VALUE}


def playwright_cookies_for_url(url: str) -> list[dict[str, Any]]:
    """Cookies in the shape Playwright's ``add_cookies`` expects.

    Returns ``[]`` for non-Curtin hosts. Domain is set to the resolved
    hostname (e.g. ``www.curtin.edu.au``) rather than a leading-dot
    apex so the cookie is scoped exactly to the host the request is
    going to — Curtin's course pages live on the ``www`` subdomain and
    the HTMX endpoint sets the cookie there too, so this matches the
    shape a real browser would persist after a real toggle click.
    """
    if not _host_matches(url):
        return []
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return []
    return [
        {
            "name": _COOKIE_NAME,
            "value": _COOKIE_VALUE,
            "domain": host,
            "path": "/",
        }
    ]
