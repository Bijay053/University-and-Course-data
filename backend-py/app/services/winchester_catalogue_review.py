"""Bounded, official-source audit for Winchester dated catalogue routes."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from app.services.scraper.challenge_shell import is_challenge_shell

_OFFICIAL_HOSTS = {"winchester.ac.uk", "www.winchester.ac.uk"}
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_YEAR_SUFFIX = re.compile(r"-(?:19|20)\d{2}$", re.I)
_AWARD = re.compile(
    r"\b(MPhil\s*/\s*PhD|PhD|MRes|MBA|MPH|MSc|MA|PGCE|PGDip|PGCert|"
    r"BA(?:\s*\(Hons\))?|BSc(?:\s*\(Hons\))?|BEd|BN|LLB|"
    r"Foundation\s+Degree|FdA|FdSc)\b",
    re.I,
)
_DISTINCT_VARIANT = re.compile(r"\b(foundation|top[\s-]?up)\b", re.I)


def _official_url(url: str) -> bool:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in _OFFICIAL_HOSTS
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


async def _assert_public_host(host: str) -> None:
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Official host did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Official host resolved to a non-public address")


def candidate_url(original_url: str) -> str | None:
    """Return a same-host yearless URL to check, never an identity rewrite."""
    if not _official_url(original_url):
        return None
    parsed = urlparse(original_url)
    parts = [part for part in parsed.path.split("/") if part]
    changed = False
    output: list[str] = []
    for part in parts:
        if _YEAR.fullmatch(part):
            changed = True
            continue
        stripped = _YEAR_SUFFIX.sub("", part)
        changed = changed or stripped != part
        output.append(stripped)
    if not changed or not output:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/" + "/".join(output) + "/", "", "", ""))


def _title_and_awards(html: str) -> tuple[str | None, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
    title = re.sub(r"\s*[-|]\s*University of Winchester\s*$", "", title, flags=re.I).strip()
    awards = []
    for match in _AWARD.finditer(title):
        award = re.sub(r"\s+", " ", match.group(1)).replace(" / ", "/")
        canonical = {
            "mphil/phd": "MPhil/PhD",
            "foundation degree": "Foundation Degree",
        }.get(award.lower(), award.upper() if len(award) <= 6 else award)
        if canonical not in awards:
            awards.append(canonical)
    return title or None, awards


async def _fetch_official(url: str) -> dict:
    if not _official_url(url):
        return {"url": url, "verified": False, "status": None, "title": None, "awards": [], "reason": "URL is not an official Winchester HTTPS URL"}
    requested_url = url
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": "UniversityCatalogueReview/1.0"},
            follow_redirects=False,
        ) as client:
            for _ in range(4):
                parsed = urlparse(current)
                await _assert_public_host(parsed.hostname or "")
                response = await client.get(current)
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = urljoin(current, response.headers.get("location", ""))
                    if not _official_url(target):
                        return {"url": requested_url, "finalUrl": current, "verified": False, "status": response.status_code, "title": None, "awards": [], "reason": "Redirect left the official Winchester host"}
                    current = target
                    continue
                if response.status_code != 200:
                    return {"url": requested_url, "finalUrl": current, "verified": False, "status": response.status_code, "title": None, "awards": [], "reason": f"Official source returned HTTP {response.status_code}"}
                html = response.text[:1_500_000]
                if is_challenge_shell(html):
                    return {"url": requested_url, "finalUrl": current, "verified": False, "status": response.status_code, "title": None, "awards": [], "reason": "Official source returned an access challenge; evidence is unverified"}
                title, awards = _title_and_awards(html)
                if not title:
                    return {"url": requested_url, "finalUrl": current, "verified": False, "status": response.status_code, "title": None, "awards": [], "reason": "Official page had no readable course title"}
                return {"url": requested_url, "finalUrl": current, "verified": True, "status": response.status_code, "title": title, "awards": awards, "reason": None}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {"url": requested_url, "finalUrl": current, "verified": False, "status": None, "title": None, "awards": [], "reason": f"Official source could not be verified: {str(exc)[:180]}"}
    return {"url": requested_url, "finalUrl": current, "verified": False, "status": None, "title": None, "awards": [], "reason": "Too many same-host redirects"}


def _comparison(original: dict, candidate: dict | None) -> tuple[str | None, str]:
    if not candidate:
        return None, "No yearless URL candidate can be derived from this route."
    if not original.get("verified") or not candidate.get("verified"):
        return None, "One or both official pages could not be verified; no counterpart is asserted."
    if re.search(r"/courses/(?:19|20)\d{2}/", original.get("url") or "", re.I):
        return None, "The original uses an explicit year-directory archive route. Matching titles do not prove that the archive and current course are interchangeable."
    original_awards = {
        value.lower() for value in (original.get("awards") or []) if isinstance(value, str)
    }
    candidate_awards = {
        value.lower() for value in (candidate.get("awards") or []) if isinstance(value, str)
    }
    if original_awards != candidate_awards:
        return "not_counterpart", "Official titles carry different awards, so they must remain distinct."
    original_title = original["title"] or ""
    candidate_title = candidate["title"] or ""
    if bool(_DISTINCT_VARIANT.search(original_title)) != bool(_DISTINCT_VARIANT.search(candidate_title)):
        return "not_counterpart", "Foundation or top-up wording differs; no counterpart match is allowed."
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", re.sub(r"\b(?:19|20)\d{2}\b", "", value.lower())).strip()
    if (
        original_awards
        and candidate_awards
        and normalize(original_title) == normalize(candidate_title)
        and original_awards == candidate_awards
    ):
        return "current_counterpart", "Official titles and awards align. This is a reviewer suggestion only; no URL or course is changed."
    if not original_awards or not candidate_awards:
        return None, "Matching titles without recognized awards do not prove a current counterpart."
    return None, "Official titles do not establish the same course; reviewer confirmation is required."


async def audit_dated_route(original_url: str) -> dict:
    checked_at = datetime.now(timezone.utc).isoformat()
    candidate = candidate_url(original_url)
    original_result, candidate_result = await asyncio.gather(
        _fetch_official(original_url),
        _fetch_official(candidate) if candidate else asyncio.sleep(0, result=None),
    )
    suggestion, reason = _comparison(original_result, candidate_result)
    return {
        "checkedAt": checked_at,
        "original": original_result,
        "candidate": candidate_result,
        "suggestion": suggestion,
        "reason": reason,
        "referenceOnly": True,
    }