"""Bounded, read-only live evidence for autonomous configuration repair.

No archive, generated code, AI extraction, or course-row writes. Direct requests
use the existing public-URL policy and refuse redirects. Explicitly configured
Scrape.do transports retain the existing provider/account safety controls.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, asdict
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.guards import (
    OBVIOUS_NON_DEGREE,
    _sanitized_visible_document,
    classify_static_course_page,
    is_blocked_page,
    is_generic_course_category_name,
)
from app.services.scraper.page_type import classify_page


_MAX_PROBE_HTML_BYTES = 1_000_000


def _is_javascript_disabled_shell(html: str) -> bool:
    """Detect a short JS-only placeholder without matching real course pages."""
    if not html or len(html) > 20_000:
        return False
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("main, article, [role=main]"):
        return False
    visible = soup.get_text(" ", strip=True)
    return bool(
        re.search(r"\bjavascript is disabled\b", visible, re.I)
        and len(visible) < 1_000
    )


async def _render_direct_shell(url: str, timeout: float) -> str | None:
    """Use the existing browser pool for a direct-request JavaScript shell."""
    if timeout <= 0:
        return None
    from app.services.scraper.recovery.extractor import _browser_fetch_html

    try:
        return await asyncio.wait_for(
            _browser_fetch_html(url, timeout_ms=max(1, int(timeout * 1_000))),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None


def bounded_limit(limits: dict, key: str, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(limits.get(key, maximum))))
    except (ValueError, TypeError, OverflowError):
        return maximum


def _bounded_html(html: str, maximum: int = _MAX_PROBE_HTML_BYTES) -> str:
    """Keep live evidence bounded without rejecting valid chrome-heavy pages."""
    encoded = html.encode("utf-8")
    if len(encoded) <= maximum:
        return html
    return encoded[:maximum].decode("utf-8", errors="ignore")


def official_url(url: str, seed: str, extra_hosts=()) -> bool:
    """Exact university host (and its www alias), never arbitrary suffixes."""
    try:
        target, origin = urlsplit(url), urlsplit(seed)
        return (
            target.scheme in ("http", "https")
            and not target.username and not target.password
            and target.port in (None, 80, 443)
            and bool(target.hostname)
            and target.hostname.lower().removeprefix("www.") in {
                (origin.hostname or "").lower().removeprefix("www."),
                *(str(host).lower().removeprefix("www.") for host in extra_hosts),
            }
        )
    except ValueError:
        return False


async def _fetch_official(url: str, config, timeout: float) -> tuple[str, str, str]:
    """Reuse the config-generator SSRF policy without unsafe redirect/fallbacks."""
    from app.services.scraper_config_ai import _is_safe_public_url

    safe, reason = await asyncio.to_thread(_is_safe_public_url, url)
    if not safe:
        return "", "network_failure" if "DNS" in reason else "unsafe_url", reason
    discovery, extraction = config.discovery, config.extraction
    render = bool(getattr(extraction, "scrape_do_render", False)
                  or getattr(discovery, "scrape_do_render", False)
                  or urlsplit(url).hostname in (
                      getattr(extraction, "scrape_do_render_hostnames", ()) or ()
                  ))
    proxy = render or bool(getattr(extraction, "scrape_do_static", False)
                           or getattr(extraction, "scrape_do_static_on_failure", False)
                           or getattr(discovery, "scrape_do_skip_fallbacks", False))
    if proxy:
        from app.services.scraper.http_fetcher import fetch_html_scrape_do, get_last_fetch_failure
        fetch_started = time.monotonic()
        html = await fetch_html_scrape_do(
            url, render=render, max_retries=0, request_timeout_seconds=timeout,
            super_mode=bool(getattr(discovery, "scrape_do_super", False)),
            geo_code=getattr(extraction, "scrape_do_geo", None) or None,
        )
        if html:
            if _is_javascript_disabled_shell(html):
                if not render:
                    remaining = timeout - (time.monotonic() - fetch_started)
                    if remaining > 0:
                        rendered = await fetch_html_scrape_do(
                            url,
                            render=True,
                            max_retries=0,
                            request_timeout_seconds=remaining,
                            super_mode=bool(getattr(discovery, "scrape_do_super", False)),
                            geo_code=getattr(extraction, "scrape_do_geo", None) or None,
                        )
                        if rendered and not _is_javascript_disabled_shell(rendered):
                            return _bounded_html(rendered), "", ""
                return "", "challenge", "JavaScript-disabled shell; live course HTML unavailable"
            return _bounded_html(html), "", ""
        failure = get_last_fetch_failure() or {}
        kind = failure.get("kind")
        classification = ("challenge" if kind == "challenge_page" else
                          "not_published" if kind == "origin_not_found" else "network_failure")
        return "", classification, f"Configured provider did not return live HTML ({kind or 'unavailable'})"
    # Unlike fetch_html(), this cannot fall through to Wayback, a paid provider,
    # or several retries. Every request consumes exactly one page-budget slot.
    insecure = set(getattr(config.discovery, "insecure_tls_direct_hostnames", []) or [])
    fetch_started = time.monotonic()
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False,
        verify=(urlsplit(url).hostname not in insecure),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        async with client.stream("GET", url) as response:
            if response.status_code in (404, 410):
                return "", "not_published", f"HTTP {response.status_code}"
            if response.status_code in (401, 403, 429):
                return "", "challenge", f"HTTP {response.status_code}; access not verified"
            if response.is_redirect:
                return "", "network_failure", "Redirect refused; destination not verified"
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "").lower():
                return "", "unsupported_content", "Live HTML evidence required"
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                remaining = _MAX_PROBE_HTML_BYTES - size
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    break
            html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            if _is_javascript_disabled_shell(html):
                remaining = timeout - (time.monotonic() - fetch_started)
                rendered = await _render_direct_shell(url, remaining)
                if rendered and not _is_javascript_disabled_shell(rendered):
                    return _bounded_html(rendered), "", ""
                return "", "challenge", "JavaScript-disabled shell; browser rendering unavailable"
            return html, "", ""


_LABEL = re.compile(
    r"\b(qualification|award|degree|duration|course length|study mode|mode of study|campus|location|"
    r"intake|start date|international (?:tuition|fee)|tuition fee|ielts|entry requirements?)\b", re.I
)
_AWARD = re.compile(
    r"\b(bachelor|master|doctor|phd|bsc|ba|beng|msc|ma|meng|mba|llb|llm|diploma|certificate)\b",
    re.I,
)
_FAILURES = {"network_failure", "challenge", "unsafe_url", "budget_exhausted", "unsupported_content"}
_REJECTED = {"listing", "non_course", "non_degree", "ineligible", "not_published"}


@dataclass(frozen=True)
class AudienceOptionEvidence:
    """Typed evidence for one audience selector option.

    ``container`` is deliberately retained: values extracted from a selector
    are only authoritative for the panel that owns that selector.  Callers
    must not flatten these records into page-wide text.
    """

    audience: str
    label: str
    value: str
    container: str
    source_url: str | None = None
    source_official: bool = False
    intake_months: tuple[int, ...] = ()
    intake_year: int | None = None


_AUDIENCE_RE = re.compile(r"\b(international|overseas|domestic|home|uk)\b", re.I)
_MONTH_NUM = {m.lower(): i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"), 1
)}


def extract_audience_option_evidence(
    html: str, page_url: str, seed_url: str, extra_hosts=()
) -> dict:
    """Extract typed selector evidence, preserving panel ownership.

    This intentionally does not search arbitrary page text.  A selector is
    usable only when every relevant option has one unambiguous audience
    identity.  Linked sources must remain on the configured official host.
    Image-only selectors and conflicting labels return ``needs_review``.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    selectors = soup.select("select, [role='listbox']")
    records: list[AudienceOptionEvidence] = []
    issues: list[str] = []
    for index, selector in enumerate(selectors):
        options = selector.select("option, [role='option']")
        # CSS-background custom controls have no trustworthy textual option
        # identity; record them for review instead of borrowing page text.
        if not options and (
            selector.get("style") and "background" in selector.get("style", "").lower()
            or any("audience" in str(cls).lower() for cls in selector.get("class", []))
        ):
            issues.append("custom image/CSS-background audience control needs review")
        relevant = []
        for option in options:
            group = option.find_parent("optgroup")
            label = " ".join(filter(None, [
                str(option.get("data-audience") or ""),
                str(option.get("data-fee-type") or ""),
                str(option.get("aria-label") or ""),
                str(option.get("title") or ""),
                " ".join(img.get("alt", "") for img in option.select("img")),
                str(group.get("label") if group else ""),
                option.get_text(" ", strip=True),
            ])).strip()
            matches = {m.casefold() for m in _AUDIENCE_RE.findall(label)}
            if not matches:
                continue
            if len(matches) > 1 or ("home" in matches and "domestic" in matches):
                issues.append("conflicting audience labels in one selector")
                continue
            audience = "international" if matches & {"international", "overseas"} else "domestic"
            text = option.get_text(" ", strip=True)
            # An image-only option has no textual identity and cannot be used.
            image_identity = bool(option.select("img")) and not text
            if image_identity or not text:
                issues.append("image-only audience option")
                continue
            source = option.get("data-source") or option.get("data-url") or option.get("href")
            source_url = urljoin(page_url, source) if source else None
            official = bool(source_url and official_url(source_url, seed_url, extra_hosts))
            if source_url and not official:
                issues.append("linked audience source is not official")
                continue
            months = tuple(sorted({
                number for name, number in _MONTH_NUM.items()
                if re.search(rf"\b{name}\b", text, re.I)
            }))
            year_match = re.search(r"\b20\d{2}\b", text)
            # Keep a real DOM identity.  nth-of-type is not stable for
            # custom listboxes (and can point at a sibling element after a
            # render); id/name/data-panel are stable ownership anchors.
            selector_id = (
                selector.get("id")
                or selector.get("name")
                or selector.get("data-panel")
                or selector.get("aria-controls")
                or f"audience-selector-{index}"
            )
            option_id = (
                option.get("value")
                or option.get("id")
                or option.get("data-value")
                or label
            )
            relevant.append(AudienceOptionEvidence(
                audience=audience, label=label, value=str(option_id),
                container=str(selector_id),
                source_url=source_url, source_official=official,
                intake_months=months,
                intake_year=int(year_match.group()) if year_match else None,
            ))
        records.extend(relevant)
    audiences = {r.audience for r in records}
    if issues:
        status = "needs_review"
    elif not records:
        status = "unconfirmed"
    elif len(audiences) > 1:
        status = "accepted"
    else:
        status = "accepted"
    return {
        "status": status,
        "evidence": [asdict(record) for record in records],
        "issues": issues,
        "same_panel": len({r.container for r in records}) <= 1,
        "linked_official": all(r.source_official or not r.source_url for r in records),
    }


def audience_scoped_recipe_proposals(evidence: dict) -> dict:
    """Build typed intake/English proposals only from linked selector evidence."""
    # Callers may provide all independently fetched page evidence under
    # ``course_evidence``. A single first-page proposal is never a safe
    # university-wide recipe.
    course_evidence = evidence.get("course_evidence")
    if course_evidence:
        if any(
            str(row.get("container") or "").startswith("audience-selector-")
            for item in course_evidence
            for row in item.get("evidence", [])
            if isinstance(row, dict)
        ):
            return {"status": "needs_review", "proposals": [], "reason": "unstable positional selector identity"}
        proposals = [
            audience_scoped_recipe_proposals(item).get("proposals", [])
            for item in course_evidence
        ]
        flat = [rows[0] for rows in proposals if rows]
        if len(flat) != len(course_evidence):
            return {"status": "needs_review", "proposals": [], "reason": "course audience evidence unresolved"}
        def signature(item):
            if any(
                str(r.get("container") or "").startswith("audience-selector-")
                for r in item.get("selectors", [])
            ):
                return None
            return (
                tuple(sorted((r.get("container"), r.get("option"), r.get("audience"),
                              tuple(r.get("intake_months") or ()), r.get("intake_year"),
                              r.get("source_url")) for r in item.get("selectors", []))),
                tuple(item.get("intake_months") or ()),
                tuple(item.get("source_urls") or ()),
                (item.get("english") or {}).get("central_page"),
            )
        if len({signature(item) for item in flat}) != 1:
            return {"status": "needs_review", "proposals": [], "reason": "conflicting course audience proposals"}
        return {"status": "accepted", "proposals": [flat[0]]}
    if evidence.get("status") != "accepted" or evidence.get("issues"):
        return {"status": "needs_review", "proposals": [], "reason": "audience evidence is unresolved"}
    rows = [r for r in evidence.get("evidence", []) if r.get("audience") == "international"]
    if not rows or not evidence.get("same_panel") or not evidence.get("linked_official"):
        return {"status": "needs_review", "proposals": [], "reason": "no same-panel official international source"}
    years = {r.get("intake_year") for r in rows if r.get("intake_year")}
    if len(years) > 1:
        return {"status": "needs_review", "proposals": [], "reason": "conflicting institutional intake years"}
    months = sorted({m for r in rows for m in (r.get("intake_months") or [])})
    source_urls = sorted({r["source_url"] for r in rows if r.get("source_url")})
    proposal = {
        "audience": "international",
        "intake_months": months,
        "source_urls": source_urls,
        "source_relationship": "selector_option",
        "selectors": [
            {
                "container": row.get("container"),
                "option": row.get("value"),
                "audience": row.get("audience"),
                "source_url": row.get("source_url"),
                "intake_months": row.get("intake_months") or [],
                "intake_year": row.get("intake_year"),
            }
            for row in rows
        ],
    }
    if source_urls:
        proposal["english"] = {
            "central_page": source_urls[0],
            "relationship": "linked_official_source",
        }
    return {"status": "accepted", "proposals": [proposal]}


def inspect_page(url: str, html: str, config=None) -> dict:
    """Classify using shared gates and visible, course-owned positive evidence."""
    from app.services.scraper.challenge_shell import is_challenge_shell

    result = {"url": url, "classification": "unconfirmed", "reason": "", "fields": [],
              "links": [], "html": html, "owned_html": ""}
    if is_challenge_shell(html):
        return {**result, "classification": "challenge", "reason": "Challenge shell, not course evidence"}
    soup = _sanitized_visible_document(html)
    extra_hosts = getattr(getattr(config, "discovery", None), "allowed_extra_hostnames", ())
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical and not official_url(urljoin(url, canonical["href"]), url, extra_hosts):
        return {**result, "classification": "non_course",
                "reason": "Cross-host canonical source; official course ownership unverified"}
    for node in soup.select("nav, footer, header, aside, [role=navigation]"):
        node.decompose()
    region = soup.select_one("main, article, [role=main]")
    if region is None:
        # Missing course scope must not turn shared footer/legal text into proof.
        page = classify_page(str(soup), url)
        return {**result, "classification": "listing" if page["course_links"] else "unconfirmed",
                "reason": "No identifiable course-owned main/article region",
                "links": page["course_links"]}
    for node in region.select(
        ".related-courses, .related, .course-list, .course-card, .partners, "
        "[class*=related-course], [class*=course-card]"
    ):
        node.decompose()
    title_node = region.find("h1")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    fields = []
    for node in region.select("dt, tr, h2, h3, p, li, [class], [id]"):
        text = node.get_text(" ", strip=True)
        if not _LABEL.search(text) or len(text) > 350:
            continue
        if node.name in ("dt", "h2", "h3"):
            sibling = node.find_next_sibling()
            if sibling:
                text += ": " + sibling.get_text(" ", strip=True)[:250]
        if not re.search(r"[:\d]", text) and len(text.split()) < 3:
            continue
        selector = node.name
        if node.get("id") and re.fullmatch(r"[\w-]+", node["id"]):
            selector += "#" + node["id"]
        elif node.get("class"):
            selector += "".join("." + cls for cls in node["class"][:2] if re.fullmatch(r"[\w-]+", cls))
        if text not in {f["text"] for f in fields}:
            fields.append({"selector": selector, "text": text[:400]})
    owned_html = str(region)
    page = classify_page(owned_html, url)
    audience_evidence = extract_audience_option_evidence(
        owned_html, url, url, extra_hosts
    )
    result.update(title=title, snippet=region.get_text(" ", strip=True)[:1400],
                  fields=fields[:12], owned_html=owned_html, links=page["course_links"],
                  audience_evidence=audience_evidence)
    blocked, reason = is_blocked_page(url, title)
    if blocked:
        return {**result, "classification": "listing" if page["page_type"] == "listing" else "non_course",
                "reason": f"Shared page gate: {reason}"}
    if re.fullmatch(r"(?:our )?(?:partners|partnerships|terms(?: and conditions)?|privacy policy)", title, re.I):
        return {**result, "classification": "non_course", "reason": "Partner directory or legal page"}
    non_degree = getattr(getattr(config, "filters", None), "non_degree", None)
    options = {
        key: getattr(non_degree, key, ())
        for key in ("allow_url_patterns", "allow_title_patterns", "force_url_patterns", "force_title_patterns")
    }
    candidate, reason, _ = classify_static_course_page(url, owned_html, **options)
    field_text = " ".join(f["text"] for f in fields)
    facts = set()
    for field in fields:
        text = field["text"]
        if (
            re.search(r"\b(?:qualification|award|degree)\b", text, re.I)
            and _AWARD.search(text)
        ):
            facts.add("award")
        if re.search(r"\b(?:duration|course length)\b.{0,40}\d.{0,15}\b(?:years?|months?|weeks?)\b", text, re.I):
            facts.add("duration")
        if re.search(r"\b(?:study mode|mode of study)\b.{0,40}\b(?:full.time|part.time|campus|blended|online)\b", text, re.I):
            facts.add("mode")
        if re.search(r"\bielts\b.{0,50}\b[4-9](?:\.\d)?\b", text, re.I):
            facts.add("english")
        if re.search(r"\binternational\b.{0,40}\b(?:fee|tuition)\b.{0,40}\d", text, re.I):
            facts.add("fee")
        if re.search(r"\b(?:campus|location)\b\s*:\s*[A-Z][a-z]{2,}", text):
            facts.add("location")
    owned_award = "award" in facts
    if candidate == OBVIOUS_NON_DEGREE and not (owned_award and reason == "non_degree_url"):
        return {**result, "classification": "non_degree", "reason": reason}
    # A title alone (including a degree title on a partner/legal page) is never
    # enough. Conversely a short/nonstandard title is not negative evidence.
    positive = len(facts) >= 2 and (
        owned_award or candidate == "likely_degree"
    )
    if is_generic_course_category_name(title) and not owned_award:
        return {**result, "classification": "listing", "reason": "Shared category gate; no course-owned award"}
    if not positive:
        return {**result, "classification": "listing" if page["course_links"] else "unconfirmed",
                "reason": "No positive course-owned award and field evidence"}
    # Explicit, course-owned negative availability is never repaired away.
    from app.services.scraper.extractors.eligibility import _NEG
    if _NEG.search(region.get_text(" ", strip=True)):
        return {**result, "classification": "ineligible", "reason": "Explicit international-audience exclusion"}
    if re.search(r"\b(?:study mode|mode of study|delivery)\b\s*[:\-]?\s*online\s+only\b", field_text, re.I):
        return {**result, "classification": "ineligible", "reason": "Explicit online-only delivery"}
    if re.search(r"\b(?:duration|study mode|mode of study)\b.{0,50}\b(?:part.time only|only (?:available )?part.time)\b", field_text, re.I):
        return {**result, "classification": "ineligible", "reason": "Explicit part-time-only route"}
    return {**result, "classification": "course",
            "reason": "Visible course-owned award/identity and multiple course fields"}


def passes(url: str, discovery: dict) -> bool:
    """All effective configurable URL gates; no raw-count success heuristic."""
    for key in ("allow_url_patterns", "course_detail_url_patterns"):
        patterns = discovery.get(key) or []
        if patterns and not any(re.search(p, url, re.I) for p in patterns):
            return False
    if any(re.search(p, url, re.I) for p in discovery.get("block_url_patterns") or []):
        return False
    must = discovery.get("must_contain") or []
    return not must or any(value.lower() in url.lower() for value in must)


def field_authority_html(field: str, html: str) -> str:
    """Keep only bounded labelled international tuition / IELTS evidence.

    A main-region domestic price or scholarship is still not tuition authority.
    Ambiguous mixed-audience blocks fail closed rather than borrowing a number.
    """
    if field not in ("international_fee", "ielts_overall"):
        return html
    soup = _sanitized_visible_document(html)
    selected = []
    for node in soup.select("p, tr, dl, section, div, li"):
        text = node.get_text(" ", strip=True)
        if len(text) > 500:
            continue
        if field == "international_fee":
            good = (
                re.search(r"\binternational\b", text, re.I)
                and re.search(r"\b(?:tuition|course fees?|annual fees?|fees?)\b", text, re.I)
                and re.search(r"\d[\d,]{2,}", text)
                and not re.search(r"\b(?:domestic|home students?|scholarship|bursary|deposit|application|accommodation|living cost|non.tuition)\b", text, re.I)
            )
        else:
            good = re.search(r"\bIELTS\b.{0,80}\b[4-9](?:\.\d)?\b", text, re.I)
        if good:
            selected.append(str(node))
    return "\n".join(selected)


class LiveRepairEvidence:
    def __init__(self, ctx: dict, limits: dict | None = None):
        limits = limits or {}
        self.ctx = ctx
        self.max_pages = bounded_limit(limits, "max_live_pages", 12)
        self.max_seconds = bounded_limit(limits, "max_live_seconds", 180)
        self.fetch_seconds = bounded_limit(limits, "fetch_timeout_seconds", 20)
        self.fetch_elapsed_seconds = 0.0
        self.pages_checked = 0
        self.records: list[dict] = []
        self.initial: dict[str, dict] = {}
        self.validation_pages: dict[str, dict] = {}
        self.config = ctx.get("effective_config")
        if self.config is None:
            from app.services.scraper.config.loader import get_config_for_host
            self.config = get_config_for_host(
                hostname=urlsplit(ctx["scrape_url"]).hostname or "",
                name=ctx["uni_name"], scrape_url=ctx["scrape_url"],
                university_id=ctx["university_id"],
                db_scrape_config=ctx.get("scrape_config_snapshot") or {},
                create_missing_stub=False,
            )
        self.extra_hosts = getattr(self.config.discovery, "allowed_extra_hostnames", ())

    async def fetch(self, url: str) -> dict:
        # The live-evidence time budget covers network activity only. AI
        # deliberation and deterministic rule replay must not consume the
        # remaining fetch allowance between probe and validation.
        remaining = self.max_seconds - self.fetch_elapsed_seconds
        record = {"url": url, "classification": "budget_exhausted", "reason": "Live page/time budget exhausted"}
        if not official_url(url, self.ctx["scrape_url"], self.extra_hosts):
            record.update(classification="unsafe_url", reason="Not an official university host")
        elif self.pages_checked < self.max_pages and remaining > 0:
            timeout = min(self.fetch_seconds, remaining)
            from app.services.worker_fencing import reserve_live_fetch, refund_live_fetch
            timeout = await reserve_live_fetch(self.max_pages, self.max_seconds, timeout)
            if timeout <= 0:
                self.records.append(record)
                return record
            self.pages_checked += 1
            token = current_uni_config.set(self.config)
            fetch_started = time.monotonic()
            try:
                html, failure, reason = await asyncio.wait_for(
                    _fetch_official(url, self.config, timeout), timeout=timeout
                )
                record = ({"url": url, "classification": failure, "reason": reason}
                          if failure else inspect_page(url, html, self.config))
            except Exception as exc:
                record.update(classification="network_failure", reason=type(exc).__name__)
            finally:
                elapsed = max(0.0, time.monotonic() - fetch_started)
                self.fetch_elapsed_seconds += elapsed
                current_uni_config.reset(token)
                await refund_live_fetch(timeout, elapsed)
        self.records.append(record)
        return record

    async def probe(self) -> dict:
        # Reserve half the entire session budget for fresh, pre-apply validation.
        limit = min(6, self.max_pages // 2)
        pending = list(dict.fromkeys(
            [self.ctx["scrape_url"]] + (self.ctx.get("passed_sample") or [])[:2]
            + (self.ctx.get("repair_course_url_sample") or [])
            + (self.ctx.get("repair_url_sample") or self.ctx.get("dropped_sample") or [])
        ))
        while pending and len(self.initial) < limit:
            url = urldefrag(pending.pop(0))[0]
            if url in self.initial:
                continue
            record = await self.fetch(url)
            self.initial[url] = record
            linked = []
            for link in record.get("links") or []:
                candidate = urljoin(url, link["url"])
                if candidate not in self.initial and candidate not in pending:
                    linked.append(candidate)
            # Official catalogue links are fresher evidence than a long stale
            # dropped-URL list. Still inspect two currently-passing candidates
            # so contamination cannot hide behind a low historical drop rate.
            index = min(2, len(pending)) if url == self.ctx["scrape_url"] else 0
            pending[index:index] = linked
        return self.audit()

    def audit(self) -> dict:
        courses = sum(r["classification"] == "course" for r in self.records)
        failures = sum(r["classification"] in _FAILURES for r in self.records)
        adequate = courses > 0 and not self.discovery_needed(self.ctx.get("effective_discovery") or {})
        return {
            "status": "accepted" if adequate else "blocked" if failures and not courses else "needs_review",
            "accepted": adequate,
            "pages_checked": self.pages_checked, "course_pages": courses,
            "rejected_pages": sum(r["classification"] in _REJECTED for r in self.records),
            "failures": failures,
            "reason": ("Bounded live evidence; full scrape verification still required" if courses
                       else "No positive live course evidence; no automatic apply is safe"),
            "samples": [{k: r.get(k) for k in ("url", "classification", "reason", "title", "snippet", "fields", "audience_evidence")}
                        for r in self.records],
        }

    def discovery_needed(self, discovery: dict) -> bool:
        known = self.ctx.get("passed_sample") or []
        return (
            not any(r["classification"] == "course" for r in self.initial.values())
            or any(r["classification"] == "course" and not passes(url, discovery)
                   for url, r in self.initial.items())
            or any(url in known and r["classification"] in _REJECTED and passes(url, discovery)
                   for url, r in self.initial.items())
            or any(url in known and r["classification"] == "unconfirmed"
                   for url, r in self.initial.items())
        )

    def discovery_validation(self, before: dict, patch: dict) -> dict:
        after = {**before, **patch}
        courses = {url for url, r in self.initial.items() if r["classification"] == "course"}
        rejected = {url for url, r in self.initial.items()
                    if r["classification"] in _REJECTED and url in (self.ctx.get("passed_sample") or [])}
        old = {url for url in courses if passes(url, before)}
        new = {url for url in courses if passes(url, after)}
        bad_old = {url for url in rejected if passes(url, before)}
        bad_new = {url for url in rejected if passes(url, after)}
        reasons = []
        if patch.get("sitemap_url") and not official_url(
            patch["sitemap_url"], self.ctx["scrape_url"], self.extra_hosts
        ):
            reasons.append("Proposed sitemap is not an official configured university source")
        if set(patch) - {"allow_url_patterns", "block_url_patterns", "must_contain", "course_detail_url_patterns"}:
            reasons.append("Discovery strategy changes require a bounded provider replay not available in this probe")
        if not new:
            reasons.append("No positive live course candidate passes proposed filters")
        if old - new:
            reasons.append("Proposed filters lose known valid course candidates")
        for url, record in self.initial.items():
            if (record["classification"] in (_FAILURES | {"unconfirmed"})
                    and url in (self.ctx.get("passed_sample") or [])
                    and passes(url, before) and not passes(url, after)):
                reasons.append("Cannot drop an unconfirmed candidate based on title or transport failure")
        if bad_new:
            reasons.append("Proposed filters still admit observed non-course candidates")
        if patch and not ((new - old) or (bad_old - bad_new)):
            reasons.append("No evidenced course rescue or contamination reduction")
        if not old and len(new) < max(1, (len(courses) + 1) // 2):
            reasons.append("Insufficient positive course rescue for total-loss discovery")
        return {"accepted": not reasons, "reasons": reasons, "courses": sorted(new),
                "preserved": sorted(old & new), "rejected_removed": sorted(bad_old - bad_new)}

    async def validate(self, discovery: dict, patch: dict, extraction: dict,
                       snapshot_validation: dict | None = None) -> dict:
        """Fresh actual fetches then pure rule replay, all before a config write."""
        report = self.discovery_validation(discovery, patch)
        reasons = report["reasons"]
        required = set(report["courses"])
        for item in (snapshot_validation or {}).get("reports") or []:
            required.update(sample["url"] for sample in item.get("samples") or [] if sample.get("url"))
        if not required:
            reasons.append("No live validation targets")
        checked = {}
        # Do not spend live-page budget on a proposal already rejected by pure
        # filter replay. This leaves the reserved validation slots available
        # for a later attempt that is actually eligible to be applied.
        if not reasons:
            for url in sorted(required):
                checked[url] = self.validation_pages.get(url) or await self.fetch(url)
                if checked[url]["classification"] not in _FAILURES:
                    self.validation_pages[url] = checked[url]
                if checked[url]["classification"] != "course":
                    reasons.append(f"Live verification failed for {url}: {checked[url]['classification']}")
        # Audience-aware recipes are eligible only when live evidence contains
        # both audiences and each selector keeps its own panel ownership.
        audience_pages = [
            page.get("audience_evidence") for page in checked.values()
            if (page.get("audience_evidence") or {}).get("evidence")
        ]
        relevant_course_pages = [
            page for page in checked.values() if page.get("classification") == "course"
        ]
        audience_required = bool(
            extraction.get("audience_repair") or extraction.get("audience_recipe")
        )
        if (
            audience_required
            and relevant_course_pages
            and len(audience_pages) != len(relevant_course_pages)
        ):
            reasons.append(
                "Audience evidence missing for one or more checked course pages; needs review"
            )
            audience_proposals = []
        audience_proposals = []
        if audience_pages:
            from app.services.scraper.recipe_rules import build_audience_scoped_recipe_proposal
            for evidence in audience_pages:
                proposal = build_audience_scoped_recipe_proposal(evidence)
                audience_proposals.extend(proposal.get("proposals") or [])
                if proposal["status"] != "accepted":
                    reasons.append(f"Audience evidence needs review: {proposal.get('reason')}")
            # A recipe is university-wide, so every checked course must agree
            # on the same typed selector relationship.  Never let the first
            # proposal win when later pages publish a different intake year,
            # source, or selector semantics.
            if audience_proposals:
                def _audience_signature(item):
                    selectors = []
                    for row in item.get("selectors") or []:
                        container = str(row.get("container") or "")
                        if container.startswith("audience-selector-"):
                            # Positional fallback identities are not stable
                            # across independently rendered course pages.
                            return None
                        selectors.append((
                            container, str(row.get("option") or ""),
                            row.get("audience"),
                            tuple(row.get("intake_months") or ()),
                            row.get("intake_year"), row.get("source_url"),
                        ))
                    return (
                        tuple(sorted(selectors)),
                        tuple(item.get("intake_months") or ()),
                        tuple(item.get("source_urls") or ()),
                        (item.get("english") or {}).get("central_page"),
                    )
                signatures = {_audience_signature(item) for item in audience_proposals}
                if None in signatures or len(signatures) != 1:
                    reasons.append(
                        "Conflicting audience proposals across checked courses; needs review"
                    )
                    audience_proposals = []
            seen = {
                row.get("audience") for evidence in audience_pages
                for row in evidence.get("evidence", [])
            }
            if not {"domestic", "international"} <= seen:
                reasons.append("Unbalanced domestic/international live audience evidence")
        if extraction:
            from app.services.scraper.ai_extractor_run import apply_extraction_rules
            from app.services.scraper.ai_repair_agent import _normalise_repair_value
            for field, rule in (extraction.get("extraction_rules") or {}).items():
                outputs = 0
                for url, page in checked.items():
                    if page["classification"] != "course":
                        continue
                    full = apply_extraction_rules(page["html"], {field: rule}).get(field, (None, ""))[0]
                    authority = field_authority_html(field, page["owned_html"])
                    owned = apply_extraction_rules(authority, {field: rule}).get(field, (None, ""))[0]
                    value = _normalise_repair_value(field, owned)
                    source_text = _sanitized_visible_document(authority).get_text(" ", strip=True).replace(",", "")
                    supported = value is not None and str(value).removesuffix(".0").lower() in source_text.lower()
                    if not supported or value != _normalise_repair_value(field, full):
                        reasons.append(f"{field}: output not supported by course-owned live content at {url}")
                        continue
                    outputs += 1
                    for item in (snapshot_validation or {}).get("reports") or []:
                        if item["field"] != field:
                            continue
                        for sample in item.get("samples") or []:
                            baseline = _normalise_repair_value(field, sample.get("before"))
                            if sample.get("url") == url and baseline is not None and baseline != value:
                                reasons.append(f"{field}: live rule changes populated baseline at {url}")
                if outputs < 2:
                    reasons.append(f"{field}: at least two positive live course outputs required")
        report["accepted"] = not reasons
        report["audience_proposals"] = audience_proposals
        report["status"] = (
            "accepted" if report["accepted"] else
            "blocked" if checked and all(page["classification"] in _FAILURES for page in checked.values())
            else "needs_review"
        )
        report["pages_checked"] = len(checked)
        return report