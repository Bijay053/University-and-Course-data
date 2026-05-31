"""
Recipe tester: lightweight discovery-only validation.

Does NOT stage courses, call Gemini, write scraped_courses rows,
run extraction, trigger publishing, or modify any existing data.

Validates in < 90 seconds whether a recipe's seed_urls / JSON API endpoint
will find courses, how many survive the must_contain + block_url_patterns
filters, and how the count compares to expected_min_courses.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from typing import Any

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("scraper.recipe_tester")

_TIMEOUT_PER_URL = 20.0
_FAKE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_URL_KEYS = ("url", "link", "href", "course_url", "page_url", "courseUrl", "pageUrl")

_COURSE_PATH_RE = re.compile(
    r"/(courses?|programs?|degrees?|study|postgraduate|undergraduate|"
    r"masters?|bachelors?|phd|doctorate|diploma|certificate|"
    r"majors?|specialisations?|qualifications?)[/-]",
    re.IGNORECASE,
)
_NON_COURSE_RE = re.compile(
    r"/(news|blog|events?|about|contact|staff|faculty|research|"
    r"library|student[-_]life|accommodation|fees[-_]funding|scholarships|"
    r"open[-_]day|clearing|apply|how[-_]to|support|help|faq|sitemap|"
    r"login|register|search|tags?|category|author|page/\d|"
    r"wp-content|wp-admin|cdn|static|assets?)\b",
    re.IGNORECASE,
)
_JS_FRAG_RE = re.compile(r"#[^/]*$")


# ── URL heuristics ────────────────────────────────────────────────────────────

def _looks_like_course(url: str, name: str = "") -> bool:
    """Lightweight course URL heuristic (mirrors browser_discover_generic logic)."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    path = parsed.path.rstrip("/")
    if not path or path in ("/", "/en"):
        return False
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return False
    if _NON_COURSE_RE.search(path):
        return False
    if _COURSE_PATH_RE.search(path):
        return True
    # Fallback: long slug with 3+ segments
    if len(parts) >= 3:
        return True
    return False


def _apply_filters(
    urls: list[str],
    must_contain: list[str],
    block_patterns: list[str],
) -> tuple[list[str], list[str]]:
    """Apply must_contain and block_url_patterns. Returns (kept, dropped)."""
    kept: list[str] = []
    dropped: list[str] = []
    block_res = [re.compile(p, re.IGNORECASE) for p in block_patterns if p]
    mc_lower = [m.lower() for m in must_contain if m]

    for url in urls:
        if mc_lower and not any(m in url.lower() for m in mc_lower):
            dropped.append(url)
            continue
        if any(r.search(url) for r in block_res):
            dropped.append(url)
            continue
        kept.append(url)
    return kept, dropped


# ── HTTP fetch + link extract ─────────────────────────────────────────────────

async def _fetch_links(
    client: httpx.AsyncClient,
    url: str,
    origin: str,
    allowed_hosts: set[str],
) -> tuple[list[str], str]:
    """Fetch a URL via plain HTTP and extract course-like links.

    Returns (links, status_note) where status_note is 'ok', 'blocked_403',
    'timeout', or 'http_NNN'.
    """
    try:
        resp = await client.get(url, timeout=_TIMEOUT_PER_URL)
        code = resp.status_code
        if code == 403:
            return [], "blocked_403"
        if code >= 400:
            return [], f"http_{code}"

        ct = resp.headers.get("content-type", "").lower()
        if "html" not in ct and "xhtml" not in ct:
            return [], f"non_html ({ct[:40]})"

        soup = BeautifulSoup(resp.text, "html.parser")
        raw: list[str] = []
        for tag in soup.find_all("a", href=True):
            href: str = tag["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            if href.startswith("/"):
                href = origin.rstrip("/") + href
            elif not href.startswith("http"):
                href = urllib.parse.urljoin(url, href)
            href = _JS_FRAG_RE.sub("", href)
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc and parsed.netloc not in allowed_hosts:
                continue
            name = (tag.get_text(strip=True) or "")[:120]
            if _looks_like_course(href, name):
                raw.append(href)

        # Deduplicate while preserving order
        seen: set[str] = set()
        links: list[str] = []
        for lk in raw:
            if lk not in seen:
                seen.add(lk)
                links.append(lk)

        return links, "ok"

    except httpx.TimeoutException:
        return [], "timeout"
    except Exception as exc:
        log.debug("_fetch_links %s: %s", url, exc)
        return [], f"error: {type(exc).__name__}"


# ── JSON API test ─────────────────────────────────────────────────────────────

async def _test_json_api(
    client: httpx.AsyncClient,
    api_cfg: dict,
    origin: str,
    warnings: list[str],
) -> dict:
    endpoint = api_cfg.get("endpoint", "")
    root_path = api_cfg.get("root_path", "")
    url_template = api_cfg.get("course_url_template", "")
    method = api_cfg.get("method", "GET").upper()
    extra_headers = dict(api_cfg.get("headers") or {})

    if not endpoint:
        return {"status": "no_endpoint", "records_found": 0, "urls_generated": 0,
                "urls": [], "sample_records": []}

    try:
        if method == "POST":
            resp = await client.post(endpoint, headers=extra_headers, timeout=_TIMEOUT_PER_URL)
        else:
            resp = await client.get(endpoint, headers=extra_headers, timeout=_TIMEOUT_PER_URL)

        if resp.status_code != 200:
            warnings.append(f"API endpoint returned HTTP {resp.status_code}")
            return {"status": f"http_{resp.status_code}", "records_found": 0,
                    "urls_generated": 0, "urls": [], "sample_records": []}

        data = resp.json()
    except httpx.TimeoutException:
        warnings.append(f"API endpoint timed out: {endpoint}")
        return {"status": "timeout", "records_found": 0, "urls_generated": 0,
                "urls": [], "sample_records": []}
    except Exception as exc:
        warnings.append(f"API endpoint error: {exc}")
        return {"status": f"error: {type(exc).__name__}", "records_found": 0,
                "urls_generated": 0, "urls": [], "sample_records": []}

    # Navigate root_path
    items: Any = data
    if root_path:
        for part in root_path.split("."):
            if isinstance(items, dict):
                items = items.get(part)
            elif isinstance(items, list) and part.isdigit():
                items = items[int(part)]
            else:
                items = None
            if items is None:
                break

    if not isinstance(items, list):
        warnings.append(
            f"API root_path '{root_path}' resolved to {type(items).__name__}, expected list. "
            "Check root_path in the JSON API config."
        )
        return {"status": "bad_root_path", "records_found": 0, "urls_generated": 0,
                "urls": [], "sample_records": []}

    # Build course URLs
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url: str | None = None
        if url_template:
            try:
                encoded = {k: urllib.parse.quote(str(v), safe="")
                           for k, v in item.items() if v is not None}
                url = url_template.format_map(encoded)
            except (KeyError, ValueError) as exc:
                log.debug("url_template format error: %s", exc)
        if not url:
            for key in _URL_KEYS:
                val = item.get(key)
                if isinstance(val, str) and val:
                    url = val
                    break
        if url:
            if url.startswith("/"):
                url = origin.rstrip("/") + url
            urls.append(url)

    # Compact sample (drop large string fields)
    sample = []
    for item in items[:3]:
        s = {k: v for k, v in item.items()
             if isinstance(v, (str, int, float, bool)) and len(str(v)) < 200
             and k not in ("content", "body", "description", "summary")}
        sample.append(s)

    return {
        "status": "ok",
        "records_found": len(items),
        "url_template": url_template or None,
        "urls_generated": len(urls),
        "urls": urls[:300],
        "sample_records": sample,
        "root_path_used": root_path or None,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

async def test_recipe(
    *,
    scrape_url: str,
    seed_urls: list[str],
    must_contain: list[str],
    block_url_patterns: list[str],
    expected_min_courses: int | None,
    json_api_cfg: dict | None,
    time_limit_s: int = 90,
) -> dict:
    """
    Run a lightweight recipe discovery test.

    Returns a structured result with:
      status           PASS / WARN / FAIL
      raw_found        total course links before filters (deduped)
      after_filter_count  links surviving must_contain + block_url_patterns
      seed_results     per-seed-URL breakdown
      api_result       JSON API test result (if configured)
      dropped_samples  first 10 dropped URLs
      kept_samples     first 10 surviving URLs
      warnings         non-fatal issues
      recommendations  actionable next steps
      elapsed_s        seconds taken
    """
    t_start = time.monotonic()

    parsed_origin = urllib.parse.urlparse(scrape_url)
    origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
    host = parsed_origin.netloc
    www_stripped = host.removeprefix("www.")
    allowed_hosts = {host, www_stripped, f"www.{www_stripped}"}

    client_headers = {
        "User-Agent": _FAKE_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }

    all_raw: list[str] = []
    seed_results: list[dict] = []
    api_result: dict | None = None
    warnings: list[str] = []
    cloudflare_blocked: int = 0

    async with httpx.AsyncClient(
        headers=client_headers,
        follow_redirects=True,
        timeout=_TIMEOUT_PER_URL,
        verify=False,
    ) as client:

        # ── 1. Test each seed URL ─────────────────────────────────────────────
        for sv in seed_urls:
            if time.monotonic() - t_start >= time_limit_s:
                warnings.append("Time limit reached — remaining seed URLs skipped")
                break

            links, status = await _fetch_links(client, sv, origin, allowed_hosts)
            seed_results.append({
                "url": sv,
                "raw_found": len(links),
                "status": status,
                "sample_urls": links[:5],
            })
            all_raw.extend(links)
            log.info("[RECIPE_TEST] Seed %s → %d links (%s)", sv, len(links), status)

            if status == "blocked_403":
                cloudflare_blocked += 1

        # ── 2. Test JSON API endpoint ─────────────────────────────────────────
        if json_api_cfg and json_api_cfg.get("endpoint"):
            api_result = await _test_json_api(client, json_api_cfg, origin, warnings)
            if api_result and api_result.get("urls"):
                all_raw.extend(api_result["urls"])

    # ── 3. Dedup and apply filters ────────────────────────────────────────────
    seen: set[str] = set()
    deduped: list[str] = []
    for u in all_raw:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    kept, dropped = _apply_filters(deduped, must_contain, block_url_patterns)
    raw_found = len(deduped)
    after_filter = len(kept)
    drop_pct = (len(dropped) / raw_found * 100) if raw_found else 0.0

    # ── 4. Verdict ────────────────────────────────────────────────────────────
    if expected_min_courses:
        if after_filter >= expected_min_courses:
            verdict = "PASS"
        elif after_filter >= max(1, int(expected_min_courses * 0.5)):
            verdict = "WARN"
        else:
            verdict = "FAIL"
    else:
        verdict = "PASS" if after_filter >= 10 else ("WARN" if after_filter >= 1 else "FAIL")

    # Override to WARN when all seeds were blocked (browser will fix this)
    if verdict == "FAIL" and cloudflare_blocked == len(seed_results) and seed_results:
        verdict = "WARN"

    # ── 5. Recommendations ───────────────────────────────────────────────────
    recommendations: list[str] = []

    if cloudflare_blocked > 0:
        recommendations.append(
            f"{cloudflare_blocked} seed URL(s) returned 403 (Cloudflare-protected). "
            "The full scrape uses a real browser which bypasses Cloudflare. "
            "HTTP test counts are 0 for those URLs — actual scrape counts will be higher. "
            "Run a full scrape to confirm."
        )

    if raw_found == 0 and not cloudflare_blocked:
        if not seed_urls and not (json_api_cfg and json_api_cfg.get("endpoint")):
            recommendations.append(
                "No seed URLs or API endpoint configured. "
                "Add course listing page URLs to discovery.seed_urls "
                "(e.g. /study/undergraduate/courses, /study/postgraduate/courses)."
            )
        else:
            recommendations.append(
                "0 course links found from seed URLs. "
                "Check that seed URLs point to course listing pages (not the homepage). "
                "Try adding must_contain=['/courses/'] or verify the URL is accessible."
            )

    if raw_found > 0 and after_filter == 0 and must_contain:
        recommendations.append(
            f"must_contain {must_contain} dropped ALL {raw_found} links. "
            "The filter substring does not appear in individual course URLs. "
            "Check whether course pages use a different path pattern."
        )
    elif drop_pct > 60 and len(dropped) >= 5:
        recommendations.append(
            f"must_contain filter removed {drop_pct:.0f}% of links ({len(dropped)} dropped). "
            "Review must_contain — it may be too strict. "
            f"Sample dropped URLs: {dropped[:3]}"
        )

    if api_result and api_result.get("records_found", 0) > 0 and api_result.get("urls_generated", 0) == 0:
        recommendations.append(
            f"API returned {api_result['records_found']} records but generated 0 course URLs. "
            "Add course_url_template to the JSON API config "
            "(e.g. https://example.com/courses/{slug} — use the JSON field names as placeholders)."
        )

    if expected_min_courses and 0 < after_filter < expected_min_courses and not cloudflare_blocked:
        recommendations.append(
            f"Found {after_filter} courses but expected {expected_min_courses}+. "
            "Add more seed URLs to cover all listing pages "
            "(undergraduate, postgraduate, research, short courses separately)."
        )

    return {
        "status": verdict,
        "expected_min_courses": expected_min_courses,
        "raw_found": raw_found,
        "after_filter_count": after_filter,
        "dropped_count": len(dropped),
        "drop_pct": round(drop_pct, 1),
        "seed_results": seed_results,
        "api_result": api_result,
        "dropped_samples": dropped[:10],
        "kept_samples": kept[:10],
        "warnings": warnings,
        "recommendations": recommendations,
        "elapsed_s": round(time.monotonic() - t_start, 1),
        "filters_applied": {
            "must_contain": must_contain,
            "block_url_patterns": block_url_patterns,
        },
    }
