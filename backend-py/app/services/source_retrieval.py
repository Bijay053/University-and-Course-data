"""Bounded binary/HTML retrieval after the caller's robots permission check.

Scrape.do is only a transport fallback for transient failures, never a way
around a refusal, challenge, unknown robots policy, or redirect.
"""
import os
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 2_000_000


def _error(message, **details):
    from app.services.scrape_assistant_tools import SourceReadError
    return SourceReadError(message, **details)


async def _bounded(response):
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            raise _error("Source exceeds the safe research size limit.")
    return bytes(raw)


def _challenge(response, raw):
    from app.services.scraper.challenge_shell import is_challenge_shell
    return (
        response.headers.get("cf-mitigated", "").lower() == "challenge"
        or (not raw.lstrip().startswith(b"%PDF-")
            and is_challenge_shell(raw[:200_000].decode("utf-8", errors="replace")))
    )


async def _read_response(response, url, *, raw=None):
    # Both provider and target redirects must be checked at a fresh permission
    # boundary, never followed automatically by either HTTP client.
    if response.is_redirect or response.headers.get("Scrape.do-Target-Redirected-Location"):
        raise _error("Source redirects; provide its final official URL.")
    raw = await _bounded(response) if raw is None else raw
    if _challenge(response, raw):
        raise _error(
            "Official source returned a protection challenge; no proxy bypass was attempted.",
            access_issue="source_access_denied", http_status=response.status_code,
            restriction_kind="cloudflare_challenge",
        )
    if response.status_code in (401, 403, 429):
        raise _error(
            f"Official source refused access (HTTP {response.status_code}); no bot-protection bypass was attempted.",
            access_issue="source_access_denied", http_status=response.status_code,
            restriction_kind="source_http_failure",
        )
    if response.status_code >= 400:
        raise _error(f"Official source returned HTTP {response.status_code}.",
                     http_status=response.status_code)
    is_pdf = raw.lstrip().startswith(b"%PDF-")
    if (urlsplit(url).path.lower().endswith(".pdf")
            or "application/pdf" in response.headers.get("content-type", "").lower()) and not is_pdf:
        raise _error("Official PDF response did not contain a valid PDF header.")
    return raw


async def _scrape_do_bytes(url, *, user_agent="StudyInfoCentre-SourceVerifier/1.0"):
    """One bounded non-rendering request; reuse account controls and redaction."""
    from app.services.scraper.http_fetcher import (
        _get_scrape_do_sem, _install_scrape_do_httpx_log_filter,
    )
    from app.services.scraper.rate_limiter import acquire_scrape_do
    from app.services.scraper.scrape_do_semaphore import account_slot

    _install_scrape_do_httpx_log_filter()
    token = os.environ.get("SCRAPE_DO_TOKEN")
    if not token:
        raise _error("Scrape.do document transport is not configured.")
    if not await acquire_scrape_do():
        raise _error("Scrape.do document request was stopped by the shared rate limit.")
    async with _get_scrape_do_sem():
        async with account_slot():
            try:
                async with httpx.AsyncClient(
                    follow_redirects=False, trust_env=False,
                    timeout=httpx.Timeout(9, connect=4),
                ) as client:
                    async with client.stream("GET", "https://api.scrape.do", params={
                        "token": token, "url": url, "render": "false",
                        "disableRedirection": "true", "customHeaders": "true",
                    }, headers={"User-Agent": user_agent}) as response:
                        return await _read_response(response, url)
            except httpx.HTTPError:
                # Provider exception URLs can include the credential.
                raise _error("Scrape.do document transport failed within its request budget.") from None


async def read_permitted_source(client, url, *, method="GET", body=None,
                                params=None, robots_allowed=False):
    """Return original bytes, not response.text or HTML-unescaped content."""
    # Preserve existing query strings, including published repeated PDF suffixes.
    target = str(httpx.URL(url).copy_merge_params(params)) if params else url
    response_received = False
    try:
        async with client.stream(method, url, json=body if method == "POST" else None,
                                 params=params or None) as response:
            response_received = True
            # Only plain temporary origin failures qualify for a transport
            # fallback. Read the body first to reject disguised challenges.
            raw = await _bounded(response)
            if response.status_code not in (502, 503, 504) or _challenge(response, raw):
                return await _read_response(response, target, raw=raw)
            if not (robots_allowed and method == "GET" and os.environ.get("SCRAPE_DO_TOKEN")):
                return await _read_response(response, target, raw=raw)
    except (httpx.TimeoutException, httpx.NetworkError):
        # A truncated body may conceal a restriction/challenge. Do not turn
        # an incomplete response into permission to try a different transport.
        if response_received or not (robots_allowed and method == "GET" and os.environ.get("SCRAPE_DO_TOKEN")):
            raise _error("Official source transport failed within its request budget.") from None
    return await _scrape_do_bytes(
        target, user_agent=client.headers.get("User-Agent", "StudyInfoCentre-SourceVerifier/1.0")
    )
