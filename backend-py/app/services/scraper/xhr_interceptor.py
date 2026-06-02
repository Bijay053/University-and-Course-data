"""Phase 4B — Autonomous API Discovery: XHR network interceptor.

Runs a short Playwright session to capture the JSON XHR/fetch calls that the
page makes during initial load.  Called as Stage 4.5 inside ``probe_site()``
when the site is a JS SPA or when Stage 4 HTML-scanning found no API signals.

The captures are passed to ``api_classifier.classify_captures()`` which
produces a ``ClassifiedAPI``, then to ``api_schema_analyzer.analyze_schema()``
which yields a field mapping stored on the ``SiteProfile`` and eventually
written into ``auto_config._field_mapping`` by the auto-config generator.

Intended trigger conditions (evaluated by the caller)
------------------------------------------------------
* ``profile.is_js_spa`` is True  — SPA that dynamically calls a backend API.
* ``len(profile.detected_apis) == 0``  — HTML scan found nothing; last resort.
* The site is accessible (``profile.static_accessible`` or bot-protected).

The whole function is best-effort: any Playwright error returns ``[]`` and
``probe_site()`` continues normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Total wall-clock budget for a single capture session
_PAGE_TIMEOUT_MS: int = 20_000
# Extra wait after domcontentloaded for async XHR traffic to arrive
_SETTLE_EXTRA_S: float = 6.0
# Hard cap on number of captures returned (largest body first)
_MAX_CAPTURES: int = 10
# Minimum JSON body size; smaller responses are likely tracking pings
_MIN_BODY_BYTES: int = 300
# Maximum body bytes we read / store (keep memory bounded)
_MAX_BODY_BYTES: int = 51_200  # 50 KB

_SKIP_URL_RE = re.compile(
    r"google-analytics|googletagmanager|google-tag|segment\.io"
    r"|facebook\.net|fbcdn|hotjar|intercom|sentry\.io|datadog"
    r"|newrelic|mixpanel|amplitude|heap\.io|fullstory"
    r"|cdn\.jsdelivr|unpkg\.com|cloudflare\.com/cdn"
    r"|fonts\.(googleapis|gstatic)"
    r"|\.woff2?(\?|$)|\.ttf(\?|$)|\.eot(\?|$)|\.otf(\?|$)"
    r"|\.css(\?|$)|\.js(\?|$)|\.png(\?|$)|\.jpg(\?|$)|\.webp(\?|$)"
    r"|\.gif(\?|$)|\.ico(\?|$)|\.svg(\?|$)|\.mp4(\?|$)|\.mp3(\?|$)"
    r"|captcha|recaptcha|turnstile",
    re.I,
)


@dataclass
class XhrCapture:
    """One intercepted XHR / fetch call made by the page during load."""

    url: str
    method: str = "GET"
    request_headers: dict[str, str] = field(default_factory=dict)
    response_status: int = 0
    content_type: str = ""
    body_size: int = 0
    # Parsed JSON body (first _MAX_BODY_BYTES); None if not valid JSON
    sample_body: Any = None
    # Authorization header value — stored separately from request_headers so
    # it is never accidentally logged.  Empty string when not present.
    _auth_header: str = field(default="", repr=False)

    def is_json(self) -> bool:
        return "json" in self.content_type.lower() or isinstance(
            self.sample_body, (dict, list)
        )

    def auth_token(self) -> str:
        """Return the raw Authorization header value (may be empty string)."""
        return self._auth_header


async def capture_xhr_signals(
    url: str,
    timeout_ms: int = _PAGE_TIMEOUT_MS,
) -> list[XhrCapture]:
    """Load *url* in headless Chromium and capture JSON XHR/fetch responses.

    Parameters
    ----------
    url:
        The university's main page / courses listing URL.
    timeout_ms:
        Hard wall-clock budget in milliseconds.

    Returns
    -------
    list[XhrCapture]
        Up to ``_MAX_CAPTURES`` captures sorted by body size descending.
        Returns ``[]`` on any Playwright error.
    """
    try:
        from playwright.async_api import (
            TimeoutError as _PwTimeout,
            async_playwright,
        )
    except ImportError:
        log.warning("[XHR] playwright not installed — skipping XHR capture")
        return []

    response_bodies: dict[str, tuple[bytes, str, int, dict]] = {}
    # url → (body, content_type, status, request_headers)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            page = await ctx.new_page()

            async def _on_response(response: Any) -> None:
                try:
                    r_url: str = response.url
                    if _SKIP_URL_RE.search(r_url):
                        return
                    resource_type: str = response.request.resource_type
                    if resource_type not in ("xhr", "fetch"):
                        return
                    ct: str = response.headers.get("content-type", "")
                    if "json" not in ct.lower():
                        return
                    status: int = response.status
                    if status < 200 or status >= 300:
                        return
                    body: bytes = await response.body()
                    if len(body) < _MIN_BODY_BYTES:
                        return
                    body_capped = body[:_MAX_BODY_BYTES]
                    # Capture request headers — cookies are stripped below before
                    # the XhrCapture is constructed.  The raw dict is only used
                    # transiently inside _on_response and never stored anywhere.
                    req_headers = dict(response.request.headers or {})
                    # Deduplicate: keep the larger body if same URL seen twice
                    existing = response_bodies.get(r_url)
                    if existing is None or len(body_capped) > len(existing[0]):
                        response_bodies[r_url] = (
                            body_capped, ct, status, req_headers,
                        )
                except Exception:
                    pass

            page.on("response", _on_response)

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                await asyncio.sleep(_SETTLE_EXTRA_S)
            except _PwTimeout:
                log.debug("[XHR] page.goto timeout — using partial captures")
            except Exception as exc:
                log.debug("[XHR] page.goto error: %s", exc)

            await browser.close()

    except Exception as exc:
        log.warning("[XHR] Playwright session failed: %s", exc)
        return []

    captures: list[XhrCapture] = []
    for resp_url, (body_bytes, ct, status, req_hdrs) in response_bodies.items():
        sample_body = None
        try:
            sample_body = json.loads(body_bytes)
        except Exception:
            pass

        captures.append(
            XhrCapture(
                url=resp_url,
                content_type=ct,
                response_status=status,
                body_size=len(body_bytes),
                sample_body=sample_body,
                request_headers={
                    k: v
                    for k, v in req_hdrs.items()
                    if k.lower() not in ("cookie", "authorization")
                },
                # auth stored separately so callers that need it can access it
                # without accidentally logging it via the normal request_headers dict.
                _auth_header=req_hdrs.get("authorization", ""),
            )
        )

    captures.sort(key=lambda c: c.body_size, reverse=True)
    result = captures[:_MAX_CAPTURES]
    log.info("[XHR] captured %d JSON calls from %s", len(result), url)
    return result
