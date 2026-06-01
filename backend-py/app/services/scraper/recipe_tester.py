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


# ── JSON API test (internal, used by test_recipe) ─────────────────────────────

_URL_KEYS = ("url", "link", "href", "course_url", "page_url", "courseUrl", "pageUrl",
             "Url", "Link", "Href")


def _navigate_path(obj: Any, path: str) -> Any:
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)]
        else:
            return None
        if obj is None:
            return None
    return obj


def _extract_names(items: list, field_map: dict) -> list[str]:
    """Extract human-readable course names from a list of JSON items."""
    name_keys = ("Title", "title", "name", "course_name", "courseName", "CourseName")
    # Add mapped field
    mapped_name_key = field_map.get("course_name")
    if mapped_name_key:
        name_keys = (mapped_name_key,) + name_keys
    names = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in name_keys:
            val = item.get(key)
            if val and isinstance(val, str) and len(val.strip()) > 2:
                names.append(val.strip())
                break
    return names


async def _test_json_api(
    client: httpx.AsyncClient,
    api_cfg: dict,
    origin: str,
    warnings: list[str],
) -> dict:
    endpoint = api_cfg.get("endpoint", "")
    root_path = api_cfg.get("root_path", "")
    count_path = api_cfg.get("count_path", "")
    url_template = api_cfg.get("course_url_template", "")
    method = api_cfg.get("method", "GET").upper()
    extra_headers = dict(api_cfg.get("headers") or {})
    query_params = dict(api_cfg.get("query_params") or {})
    pagination = dict(api_cfg.get("pagination") or {})
    page_param = pagination.get("page_param", "page")
    page_size_raw = pagination.get("page_size")
    size_param = pagination.get("size_param", "limit")
    page_start = int(pagination.get("page_start", 0))
    field_map = dict(api_cfg.get("fields") or {})

    if not endpoint:
        return {"status": "no_endpoint", "records_found": 0, "urls_generated": 0,
                "urls": [], "sample_records": []}

    # Build page-1 params
    fetch_params: dict = dict(query_params)
    if pagination.get("type") == "offset":
        fetch_params[page_param] = page_start
        if page_size_raw is not None:
            fetch_params[size_param] = int(page_size_raw)

    try:
        if method == "POST":
            resp = await client.post(endpoint, json=fetch_params or None,
                                     headers=extra_headers, timeout=_TIMEOUT_PER_URL)
        else:
            resp = await client.get(endpoint, params=fetch_params or None,
                                    headers=extra_headers, timeout=_TIMEOUT_PER_URL)

        if resp.status_code != 200:
            warnings.append(f"API endpoint returned HTTP {resp.status_code}")
            return {"status": f"http_{resp.status_code}", "http_status": resp.status_code,
                    "records_found": 0, "urls_generated": 0, "urls": [], "sample_records": []}

        data = resp.json()
    except httpx.TimeoutException:
        warnings.append(f"API endpoint timed out: {endpoint}")
        return {"status": "timeout", "records_found": 0, "urls_generated": 0,
                "urls": [], "sample_records": []}
    except Exception as exc:
        warnings.append(f"API endpoint error: {exc}")
        return {"status": f"error: {type(exc).__name__}", "records_found": 0,
                "urls_generated": 0, "urls": [], "sample_records": []}

    # Read total count from count_path
    total_from_api: int | None = None
    if count_path:
        total_raw = _navigate_path(data, count_path)
        try:
            total_from_api = int(total_raw)
        except (TypeError, ValueError):
            warnings.append(f"count_path '{count_path}' returned {type(total_raw).__name__}, expected int")

    # Navigate root_path to course list
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
                "urls": [], "sample_records": [], "http_status": 200,
                "total_from_api": total_from_api, "page1_count": 0}

    page1_count = len(items)
    page1_names = _extract_names(items, field_map)

    # Optionally fetch page 2 to verify pagination
    page2_count: int | None = None
    if pagination.get("type") == "offset" and page1_count > 0:
        p2_params = dict(fetch_params)
        p2_params[page_param] = page_start + 1
        try:
            if method == "POST":
                r2 = await client.post(endpoint, json=p2_params, headers=extra_headers,
                                       timeout=_TIMEOUT_PER_URL)
            else:
                r2 = await client.get(endpoint, params=p2_params, headers=extra_headers,
                                      timeout=_TIMEOUT_PER_URL)
            if r2.status_code == 200:
                d2 = r2.json()
                its2 = _navigate_path(d2, root_path) if root_path else d2
                page2_count = len(its2) if isinstance(its2, list) else 0
        except Exception:
            pass

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
        "http_status": 200,
        "records_found": len(items),
        "url_template": url_template or None,
        "urls_generated": len(urls),
        "urls": urls[:300],
        "sample_records": sample,
        "root_path_used": root_path or None,
        "total_from_api": total_from_api,
        "page1_count": page1_count,
        "page2_count": page2_count,
        "sample_names": page1_names[:8],
    }


# ── Standalone JSON API test (called by the /recipe/test-api route) ────────────

async def test_json_api_standalone(api_cfg: dict) -> dict:
    """Test a JSON API endpoint configuration directly without needing a scrape_url.

    Returns a rich result dict suitable for the Recipe Editor's Test API panel:
      status          'ok' | 'no_endpoint' | 'timeout' | 'http_NNN' | 'bad_root_path' | 'error:…'
      http_status     HTTP status code (int)
      total_from_api  Total count from count_path if configured (int | null)
      page1_count     Courses on page 1 (int)
      page2_count     Courses on page 2 — only when pagination.type=offset (int | null)
      sample_names    Up to 8 course names from page 1 (list[str])
      all_keys        All top-level JSON keys from first item (list[str])
      warnings        Non-fatal issues (list[str])
    """
    endpoint = (api_cfg.get("endpoint") or "").strip()
    if not endpoint:
        return {"status": "no_endpoint", "http_status": None, "total_from_api": None,
                "page1_count": 0, "page2_count": None, "sample_names": [], "all_keys": [],
                "warnings": ["No API endpoint configured"]}

    parsed = urllib.parse.urlparse(endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    warnings: list[str] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await _test_json_api(client, api_cfg, origin, warnings)

    # Add all_keys from first sample record
    all_keys: list[str] = []
    if result.get("sample_records"):
        all_keys = list(result["sample_records"][0].keys())

    result["all_keys"] = all_keys
    result["warnings"] = warnings
    return result


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
    # Detect "nothing to test" before scoring — no seeds AND no API endpoint
    # means the tester had nothing to fetch.  This is NOT a test failure; it
    # means the recipe is not yet configured.  Show NOT_CONFIGURED (neutral)
    # rather than FAIL so operators know to add seed URLs, not debug a failure.
    _no_input = (
        not seed_urls
        and not (json_api_cfg and json_api_cfg.get("endpoint"))
    )

    if _no_input:
        verdict = "NOT_CONFIGURED"
    elif expected_min_courses:
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

    # discovery_incomplete: True when found < expected_min_courses and the
    # site is not Cloudflare-blocked.  The frontend uses this to surface a
    # prominent "only X found, expected 100+" warning with the prompt
    # "run extraction anyway?" so operators know to investigate seeds first.
    _discovery_incomplete = bool(
        expected_min_courses
        and 0 < after_filter < expected_min_courses
        and cloudflare_blocked == 0
    )
    _discovery_incomplete_message = (
        f"Discovery incomplete: found {after_filter} courses but expected "
        f"{expected_min_courses}+. Check that seed URLs point to the full "
        "course listing pages, not hub/overview pages."
        if _discovery_incomplete else None
    )

    return {
        "status": verdict,
        "expected_min_courses": expected_min_courses,
        "raw_found": raw_found,
        "after_filter_count": after_filter,
        "dropped_count": len(dropped),
        "drop_pct": round(drop_pct, 1),
        # Explicit list of the seed URLs that were submitted for testing.
        # Shown in the UI as "Configured seed URLs" so the operator can
        # verify the exact URLs tested match what they entered.
        "configured_seed_urls": list(seed_urls),
        "seed_results": seed_results,
        "discovery_incomplete": _discovery_incomplete,
        "discovery_incomplete_message": _discovery_incomplete_message,
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
