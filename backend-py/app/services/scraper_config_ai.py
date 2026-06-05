"""AI-assisted per-university scraper YAML generator.

Given a few inputs (slug, university name, homepage, course-listing URL,
optional sample course URL) this module asks Gemini to produce a complete
``scraper_config/unis/<slug>.yaml`` that conforms to the per-uni override
schema (``DiscoveryConfig`` + ``ExtractionConfig``).

Design goals
------------
* One-shot generation: a single Gemini call with a tight, schema-aware prompt
  and a few high-quality exemplars from the existing YAML library.
* Optional best-effort fetch of the listing page (short timeout, scripts/styles
  stripped, truncated to ~12 kB) so Gemini can see the actual link shapes
  on the site and propose realistic ``allow_url_patterns``.
* Output is validated TWICE before being returned:
    1. ``yaml.safe_load`` — must parse to a top-level mapping.
    2. ``DiscoveryConfig`` + ``ExtractionConfig`` Pydantic models — every
       provided sub-section must conform to the production schema, so the
       returned YAML is guaranteed to load via ``loader.load_uni_config``
       without raising a validation error at scrape time.
* On schema failure the generator retries ONCE with the validation error
  fed back into the prompt so Gemini can self-correct.
* Generated content is NEVER persisted by this module — the caller (router)
  decides whether to save it.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import ValidationError

from app.services.ai import gemini_client
from app.services.scraper.config.schema import DiscoveryConfig, ExtractionConfig

log = logging.getLogger("uniportal.scraper_config_ai")

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_DIR = _BACKEND_ROOT / "scraper_config"
_TEMPLATE_FILE = _CONFIG_DIR / "_template.yaml"
_DEFAULTS_FILE = _CONFIG_DIR / "defaults.yaml"
_UNIS_DIR = _CONFIG_DIR / "unis"

# A small curated set of well-tuned existing YAMLs.  Gemini sees these as
# "good examples" so the output style matches the rest of the library
# (heavy commenting documenting the WHY of every override, plus realistic
# regex / URL patterns).
_EXEMPLAR_SLUGS: tuple[str, ...] = ("uow", "federation", "utas")

# Keep the prompt under Gemini's input budget; the listing-page snapshot is
# the single biggest input so it gets a hard cap.  Extractor pages can be
# 200 kB raw HTML; 12 kB of cleaned text is plenty for "show me the link
# shapes on this listing page".
_MAX_HTML_CHARS = 12_000

_FENCE_RE = re.compile(r"^```(?:ya?ml)?\s*\n?|\n?```\s*$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

class GenerationError(RuntimeError):
    """Raised when YAML generation fails irrecoverably."""


async def generate_scraper_yaml(
    *,
    slug: str,
    name: str,
    homepage: str | None = None,
    course_listing_url: str | None = None,
    sample_course_url: str | None = None,
    extra_notes: str | None = None,
) -> dict[str, Any]:
    """Generate a complete per-uni YAML using Gemini.

    Returns ``{"content": <yaml str>, "cost_usd": float, "fetched_url": str|None,
    "warnings": list[str]}``.

    Raises :class:`GenerationError` when:
      * Gemini is unavailable (no API key, circuit open, empty response).
      * Output cannot be made schema-valid after one self-correction retry.
    """
    warnings: list[str] = []

    # 1. Optional listing-page fetch
    listing_text, fetched_url = await _maybe_fetch_listing(
        course_listing_url, warnings=warnings
    )

    # 2. Build prompt context
    template = _read_safe(_TEMPLATE_FILE)
    defaults = _read_safe(_DEFAULTS_FILE)
    exemplars = _load_exemplars()

    base_prompt = _build_prompt(
        slug=slug,
        name=name,
        homepage=homepage,
        course_listing_url=course_listing_url,
        sample_course_url=sample_course_url,
        extra_notes=extra_notes,
        listing_text=listing_text,
        template=template,
        defaults=defaults,
        exemplars=exemplars,
    )

    # 3. First attempt
    yaml_text, cost1 = await _call_gemini(base_prompt, call_type="config_gen")
    error = _validate_uni_yaml(yaml_text)
    if error is None:
        try:
            _parsed = yaml.safe_load(yaml_text) or {}
            warnings.extend(_yaml_improvement_warnings(_parsed))
        except Exception:
            pass
        return {
            "content": yaml_text,
            "cost_usd": cost1,
            "fetched_url": fetched_url,
            "warnings": warnings,
        }

    # 4. Self-correction retry — feed Gemini its own validation error.
    log.info("scraper_config_ai: retrying after validation error: %s", error)
    warnings.append(f"first attempt rejected ({error}); retried once")
    retry_prompt = (
        base_prompt
        + "\n\n# ── PRIOR ATTEMPT REJECTED ─────────────────────────────────\n"
        f"# Your previous output failed schema validation:\n#   {error}\n"
        "# Re-emit the YAML with that issue fixed.  Output YAML only,\n"
        "# no markdown fences.\n"
    )
    yaml_text2, cost2 = await _call_gemini(retry_prompt, call_type="config_gen_retry")
    error2 = _validate_uni_yaml(yaml_text2)
    if error2 is not None:
        raise GenerationError(
            f"Gemini output failed schema validation after retry: {error2}"
        )
    try:
        _parsed2 = yaml.safe_load(yaml_text2) or {}
        warnings.extend(_yaml_improvement_warnings(_parsed2))
    except Exception:
        pass
    return {
        "content": yaml_text2,
        "cost_usd": cost1 + cost2,
        "fetched_url": fetched_url,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_uni_yaml(text: str) -> str | None:
    """Return None if ``text`` is a valid per-uni YAML, else the error message.

    Per-uni YAMLs only carry ``discovery:`` and ``extraction:`` sections
    (slug/name/base_url/scrape_url are injected by the loader), so we
    validate just those sub-models.
    """
    if not text or not text.strip():
        return "empty output"
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return f"YAML parse error: {e}"
    if parsed is None:
        return "empty document"
    if not isinstance(parsed, dict):
        return "top-level must be a mapping"

    allowed_top = {"discovery", "extraction"}
    extra = set(parsed.keys()) - allowed_top
    if extra:
        return f"unsupported top-level key(s): {sorted(extra)}"

    disc = parsed.get("discovery") or {}
    ext = parsed.get("extraction") or {}
    if disc and not isinstance(disc, dict):
        return "discovery: must be a mapping"
    if ext and not isinstance(ext, dict):
        return "extraction: must be a mapping"
    try:
        DiscoveryConfig.model_validate(disc)
    except ValidationError as e:
        return f"discovery section invalid: {_first_pydantic_error(e)}"
    try:
        ExtractionConfig.model_validate(ext)
    except ValidationError as e:
        return f"extraction section invalid: {_first_pydantic_error(e)}"
    return None


# Broad patterns that can accidentally admit search/listing pages into extraction.
# When allow_url_patterns matches these but course_detail_url_patterns is empty,
# the operator is warned to add the post-discovery gate.
_BROAD_ALLOW_PATTERNS: tuple[str, ...] = (
    "/courses/", "/course/", "/study/", "/programs/", "/programme/",
    "/degrees/", "/undergraduate/", "/postgraduate/",
)

# Regex-metachar check: if a pattern contains any of these it is already
# specific enough that the broad-path warning should NOT fire.
_HAS_REGEX_CHARS = re.compile(r'[\[\]()+*?\\]|\.\d|\|')


def _yaml_improvement_warnings(parsed: dict) -> list[str]:
    """Return human-readable warnings for valid but potentially risky YAML configs.

    These are *soft* advisories, not errors — the YAML will still be accepted.
    Called after schema validation passes so the parsed dict is already trusted.
    """
    warnings: list[str] = []
    disc = parsed.get("discovery") or {}

    allow_pats = list(disc.get("allow_url_patterns") or [])
    cdp = list(disc.get("course_detail_url_patterns") or [])

    # Warn when allow_url_patterns is broad but course_detail_url_patterns is absent.
    # Only flag patterns that are plain path prefixes (no regex metacharacters) — a
    # pattern like `/courses/[^/]+-\d` is already specific enough and doesn't need
    # the extra post-discovery gate.
    if allow_pats and not cdp:
        broad = [
            p for p in allow_pats
            if any(b in p for b in _BROAD_ALLOW_PATTERNS)
            and not _HAS_REGEX_CHARS.search(p)
        ]
        if broad:
            warnings.append(
                "Broad allow_url_patterns may allow search/listing pages into extraction "
                f"(e.g. {broad[0]!r}). Add discovery.course_detail_url_patterns to restrict "
                "extraction to genuine course-detail URLs and avoid fake-course staging."
            )

    return warnings


def _first_pydantic_error(e: ValidationError) -> str:
    errs = e.errors()
    if not errs:
        return str(e)
    err = errs[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "invalid")
    return f"{loc}: {msg}" if loc else msg


# ---------------------------------------------------------------------------
# Listing-page fetch
# ---------------------------------------------------------------------------

def _is_safe_public_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only allow http(s) URLs that resolve to public IP space.

    Blocks private / loopback / link-local / multicast / reserved ranges so
    an admin can't (accidentally or otherwise) point the generator at
    127.0.0.1, the cloud metadata endpoint (169.254.169.254), or RFC1918
    internal services.
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return False, f"invalid URL: {e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"only http/https supported (got {parsed.scheme!r})"
    host = parsed.hostname
    if not host:
        return False, "URL is missing a hostname"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"DNS lookup failed: {e}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"invalid resolved address {ip_str!r}"
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"resolves to non-public address {ip_str}"
    return True, ""


async def _maybe_fetch_listing(
    url: str | None, *, warnings: list[str]
) -> tuple[str | None, str | None]:
    """Fetch the listing page and return cleaned text, or (None, None).

    Best-effort: any failure (SSRF guard, network, HTTP error) records a
    warning and returns no content — generation continues without it.
    """
    if not url:
        return None, None
    safe, reason = _is_safe_public_url(url)
    if not safe:
        warnings.append(f"listing fetch refused: {reason}")
        return None, url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            # follow_redirects=False prevents a public URL from bouncing
            # the request to a private one and bypassing the SSRF guard.
            follow_redirects=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                    "Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as c:
            r = await c.get(url)
            if r.status_code >= 400:
                warnings.append(f"listing fetch returned HTTP {r.status_code}")
                return None, str(r.url)
            html = r.text or ""
    except Exception as exc:  # noqa: BLE001 — best-effort
        warnings.append(f"listing fetch failed: {exc.__class__.__name__}: {exc}")
        return None, None

    cleaned = _strip_html(html)
    if len(cleaned) > _MAX_HTML_CHARS:
        cleaned = cleaned[:_MAX_HTML_CHARS] + "\n[…truncated]"
    return cleaned, url


_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")


def _strip_html(html: str) -> str:
    """Very small HTML→text cleaner.  Preserves anchor hrefs as ``[text](href)``
    so Gemini can see the link shape on the page.
    """
    body = _SCRIPT_RE.sub("", html)
    # Surface anchors so the model can see URL patterns
    body = re.sub(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        lambda m: f"[{re.sub(r'<[^>]+>', '', m.group(2)).strip()}]({m.group(1)})",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body)
    body = _BLANK_RE.sub("\n\n", body)
    return body.strip()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _read_safe(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_exemplars() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for slug in _EXEMPLAR_SLUGS:
        p = _UNIS_DIR / f"{slug}.yaml"
        body = _read_safe(p)
        if body:
            # Cap any single exemplar to keep the prompt reasonable
            if len(body) > 6000:
                body = body[:6000] + "\n# [...truncated for brevity]\n"
            out.append((slug, body))
    return out


def _build_prompt(
    *,
    slug: str,
    name: str,
    homepage: str | None,
    course_listing_url: str | None,
    sample_course_url: str | None,
    extra_notes: str | None,
    listing_text: str | None,
    template: str,
    defaults: str,
    exemplars: list[tuple[str, str]],
) -> str:
    parts: list[str] = []
    parts.append(
        "You are an expert configuration engineer for a university-course "
        "web-scraping system. Produce a COMPLETE per-university YAML override "
        "file for the university described below.\n"
        "\n"
        "OUTPUT CONTRACT (read carefully):\n"
        "  * Output ONLY YAML — no markdown fences, no prose before or after.\n"
        "  * Top-level keys MUST be a subset of: `discovery`, `extraction`.\n"
        "  * Anything you don't override INHERITS from defaults.yaml — keep the\n"
        "    file MINIMAL: only emit overrides that are genuinely useful for\n"
        "    THIS university. Do NOT restate defaults.\n"
        "  * Every override MUST be accompanied by a short `# comment` line\n"
        "    above it explaining WHY it is needed (cite a specific page\n"
        "    behaviour, URL shape, or known quirk you observed).\n"
        "  * Regex values go in DOUBLE QUOTES so YAML doesn't choke on them.\n"
        "  * Use realistic patterns derived from the listing page if provided —\n"
        "    do NOT invent URL paths that aren't visible on the site.\n"
        "  * If you have nothing meaningful to override in a section, OMIT the\n"
        "    section entirely rather than emitting `{}`.\n"
        "  * The output will be parsed by yaml.safe_load and validated against\n"
        "    a strict Pydantic schema. Unknown keys cause a validation error.\n"
        "\n"
        "FIELD CHEATSHEET (only emit fields that solve a real problem):\n"
        "  discovery:\n"
        "    bfs_page_budget: int — raise above 25 only when the catalogue has\n"
        "      many paginated listing pages.\n"
        "    fallback_subdomains: [str] — probed when primary URL yields <5\n"
        "      candidates. Use `{domain}` placeholder. Example:\n"
        "      ['handbook.{domain}', 'study.{domain}'].\n"
        "    always_sitemap_supplement: bool — true for JS-rendered SPAs\n"
        "      (Torrens, CDU) where BFS misses content.\n"
        "    always_browser_discover: bool — true for Cloudflare-protected\n"
        "      sites (UTAS-style) where BFS silently misses faculties.\n"
        "    block_url_patterns: [regex] — drop info/marketing pages BEFORE\n"
        "      allow patterns. E.g. '/study/scholarships', '/study/apply'.\n"
        "    allow_url_patterns: [regex] — whitelist; only URLs matching at\n"
        "      least one regex are kept. E.g. '/study/courses/' or\n"
        "      '/courses/[^/]+/courses/'.\n"
        "    must_contain: [str] — substring whitelist; simpler than regex.\n"
        "    sitemap_url: str — explicit sitemap override.\n"
        "    extra_course_urls: [str] — surgical URL injection for known\n"
        "      courses BFS / sitemap consistently miss.\n"
        "    use_wayback: bool — Wayback fallback for WAF-blocked sites.\n"
        "  extraction:\n"
        "    fees:\n"
        "      central_page: url; fees_pdf_url: url;\n"
        "      default_currency: 'AUD' (or 'NZD' for NZ unis);\n"
        "      course_pdf_aliases: {db_lower: pdf_title};\n"
        "      force_central_fee_stage: bool;\n"
        "    english:\n"
        "      central_page: url; requirements_pdf_url: url;\n"
        "      trust_vision_ocr: bool (false for unis prone to OCR\n"
        "      hallucinations like ACAP);\n"
        "      test_blocklist: ['kite','duolingo'];\n"
        "      default_ielts/pte/toefl: float (last-resort default).\n"
        "    filters:\n"
        "      domestic_only.enabled: bool — true for unis without an intl tab;\n"
        "      online_only.enabled: bool — set false for distance-ed unis (CSU).\n"
        "    url_rewrites: [{host, path_contains, append_query}] — append\n"
        "      query params before fetching course pages (e.g.\n"
        "      'audience=INTERNATIONAL', 'students=international&year=2026').\n"
        "    text_cleaning.global_substring_blocklist: [str] — strip\n"
        "      boilerplate ('Apply Now', 'Click here to enquire').\n"
        "    text_cleaning.field_overrides: [{url_regex, field, value}] —\n"
        "      surgical hard overrides for specific URLs.\n"
        "    default_course_location: str — fallback when extractor returns\n"
        "      blank (used by online-guard).\n"
        "    max_parallel_fetch: int — cap browser concurrency (1 for unis\n"
        "      that aggressively rate-limit, e.g. UTAS).\n"
        "\n"
        "DESIGN HEURISTICS:\n"
        "  * If the listing-page snapshot shows clear `/courses/` or\n"
        "    `/study/courses/` URL paths, propose a matching `allow_url_pattern`\n"
        "    and (if you can see info pages mixed in) `block_url_patterns`.\n"
        "  * If the homepage / listing URL contains 'international' as a\n"
        "    query param or path segment hint, consider a `url_rewrites`\n"
        "    entry to make the international fee/IELTS/intake visible.\n"
        "  * Australian universities default to AUD; NZ universities to NZD.\n"
        "  * Online-only delivery is rejected by default. Set\n"
        "    `extraction.filters.online_only.enabled: false` for clearly\n"
        "    distance-education-heavy providers.\n"
        "  * Be CONSERVATIVE — too few overrides is far better than wrong\n"
        "    ones. The defaults already work well for the median university.\n"
    )

    parts.append("\n# ── INPUTS ─────────────────────────────────────────────\n")
    parts.append(f"university_name: {name}\n")
    parts.append(f"slug: {slug}\n")
    if homepage:
        parts.append(f"homepage: {homepage}\n")
    if course_listing_url:
        parts.append(f"course_listing_url: {course_listing_url}\n")
    if sample_course_url:
        parts.append(f"sample_course_url: {sample_course_url}\n")
    if extra_notes:
        parts.append(f"\noperator_notes:\n{extra_notes}\n")

    parts.append(
        "\n# ── DEFAULTS (read-only baseline you DO NOT need to restate) ──\n"
    )
    parts.append(defaults)

    parts.append(
        "\n# ── TEMPLATE (shape & comment style to match) ─────────────────\n"
    )
    parts.append(template)

    for slug_ex, body in exemplars:
        parts.append(
            f"\n# ── EXEMPLAR: {slug_ex}.yaml ──────────────────────────────\n"
        )
        parts.append(body)

    if listing_text:
        parts.append(
            "\n# ── LIVE LISTING-PAGE SNAPSHOT (cleaned, anchors preserved) ─\n"
            "# Use this to derive realistic allow_url_patterns / "
            "block_url_patterns and to spot international-tab toggles.\n"
        )
        parts.append(listing_text)

    parts.append(
        "\n# ── NOW EMIT THE FILE ─────────────────────────────────────────\n"
        "# Begin output with a header comment line `# <University Name> "
        f"({slug})`\n"
        "# followed by the YAML body. NO markdown fences. NO prose.\n"
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Gemini call + post-processing
# ---------------------------------------------------------------------------

async def _call_gemini(prompt: str, *, call_type: str) -> tuple[str, float]:
    resp = await gemini_client.generate(
        prompt, max_output_tokens=4096, call_type=call_type
    )
    if resp.skipped:
        raise GenerationError(
            f"Gemini call skipped: {resp.skip_reason or 'unknown reason'}"
        )
    if not resp.text:
        raise GenerationError("Gemini returned an empty response")
    return _post_process(resp.text), float(resp.cost_usd or 0.0)


def _post_process(text: str) -> str:
    """Strip ```yaml fences and ensure a trailing newline."""
    body = text.strip()
    body = _FENCE_RE.sub("", body).strip()
    if not body.endswith("\n"):
        body += "\n"
    return body


# ---------------------------------------------------------------------------
# Category-landing-page rejection analyser
# ---------------------------------------------------------------------------

def analyze_category_landing_rejections(
    skip_reasons: dict[str, int],
    skip_reason_samples: dict[str, list[dict]],
) -> list[dict[str, Any]]:
    """Analyse ``category_landing_page_*`` skip-reason counts and return a list
    of actionable YAML fix recommendations for the operator.

    Each item in the returned list is a dict with keys:
      ``sub_reason``  — the granular reason key (e.g. ``category_landing_page_missing_degree_qualifier``)
      ``count``       — number of pages rejected for this reason
      ``samples``     — list of {url, name} dicts (up to 10)
      ``severity``    — ``"high"`` (≥10) / ``"medium"`` (3-9) / ``"low"`` (<3)
      ``recommendation`` — human-readable YAML fix suggestion

    Only sub-reasons with count > 0 are included.  Sub-reasons with no YAML
    fix (``url_block``, ``title_block``) receive an ``"informational"``
    recommendation explaining that the guard is correct.
    """
    results: list[dict[str, Any]] = []

    _CATEGORY_SUB_REASONS = (
        "category_landing_page_missing_degree_qualifier",
        "category_landing_page_url_block",
        "category_landing_page_title_block",
        "category_landing_page_url_suffix",
    )

    for sub_reason in _CATEGORY_SUB_REASONS:
        count = skip_reasons.get(sub_reason, 0)
        if count == 0:
            continue
        samples = skip_reason_samples.get(sub_reason, [])
        severity = "high" if count >= 10 else ("medium" if count >= 3 else "low")

        if sub_reason == "category_landing_page_missing_degree_qualifier":
            # Sample names help the operator decide whether to relax the gate.
            sample_names = [s.get("name", "") for s in samples if s.get("name")]
            name_preview = ", ".join(f'"{n}"' for n in sample_names[:3])
            recommendation = (
                f"{count} course(s) were rejected because the extracted H1/title "
                "lacks a recognised degree-level qualifier (Bachelor, Master, Diploma, etc.).\n"
                "Sample names: " + (name_preview or "(none captured)") + "\n\n"
                "Fix options:\n"
                "  A) If this university uses non-standard degree names (e.g. short "
                "     professional awards), add to extraction.staging:\n"
                "       skip_degree_qualifier_check: true\n"
                "     This suppresses the guard for this university only.\n"
                "  B) If the names look like generic nav-text (e.g. 'Study online', "
                "     'Postgraduate study') the guard is working correctly — "
                "     tighten allow_url_patterns or add block_url_patterns instead.\n"
                "  C) If names look like real awards but the qualifier is missing "
                "     (e.g. titles scraped from a listing card with no H1), check "
                "     extraction.html.h1_selectors to ensure the correct heading is "
                "     targeted."
            )

        elif sub_reason == "category_landing_page_url_block":
            sample_urls = [s.get("url", "") for s in samples if s.get("url")]
            url_preview = "\n    ".join(sample_urls[:5])
            # Find common path prefix across samples to suggest a tighter pattern.
            _common_prefix = _common_url_path_prefix(sample_urls)
            prefix_hint = (
                f"\n  Common path prefix: {_common_prefix!r}" if _common_prefix else ""
            )
            recommendation = (
                f"{count} URL(s) matched a globally-blocked path pattern "
                "(category/listing hub, not a course detail).\n"
                "Sample URLs:\n"
                f"    {url_preview}\n"
                f"{prefix_hint}\n\n"
                "Fix options:\n"
                "  A) Informational — these rejections are expected; the global URL "
                "     block list is catching navigation/category hubs correctly.\n"
                "  B) If legitimate course URLs share the blocked path segment, add a "
                "     host exception in guards._BLOCK_URL_SUBSTRINGS_HOST_EXCEPTIONS "
                "     or tighten discovery.allow_url_patterns to only emit real "
                "     course-detail paths so those pages are never enqueued.\n"
                "  C) Add discovery.block_url_patterns entries to exclude the specific "
                f"     hub prefix ({_common_prefix!r}) at discovery time rather than "
                "     at staging, reducing wasted extraction work."
            )

        elif sub_reason == "category_landing_page_title_block":
            sample_names = [s.get("name", "") for s in samples if s.get("name")]
            name_preview = ", ".join(f'"{n}"' for n in sample_names[:5])
            recommendation = (
                f"{count} page(s) were rejected because the page title matched a "
                "known category-listing prefix (e.g. 'Undergraduate study', "
                "'Browse courses', 'Study online').\n"
                "Sample titles: " + (name_preview or "(none captured)") + "\n\n"
                "Fix options:\n"
                "  A) Informational — the title-block guard is working correctly; "
                "     these are listing/hub pages, not course details.\n"
                "  B) If real course titles are being caught, check "
                "     extraction.html.h1_selectors — the scraper may be picking up "
                "     the page <title> or a nav heading instead of the course H1. "
                "     Add a more specific h1_selectors CSS path."
            )

        elif sub_reason == "category_landing_page_url_suffix":
            sample_urls = [s.get("url", "") for s in samples if s.get("url")]
            url_preview = "\n    ".join(sample_urls[:3])
            recommendation = (
                f"{count} URL(s) ended with a known shared-specialisation suffix "
                "(/two-specialisations, /two-specializations).\n"
                "Sample URLs:\n"
                f"    {url_preview}\n\n"
                "Fix options:\n"
                "  A) Informational — these are specialisation-picker hub pages; "
                "     the guard is correct.\n"
                "  B) If this university embeds real per-specialisation fee/IELTS "
                "     data on these pages and you want them staged, add the suffix "
                "     to discovery.allow_url_patterns with a specific regex that "
                "     anchors on the course slug before the suffix, then remove the "
                "     suffix from guards._CATEGORY_URL_SUFFIXES for this host via "
                "     a YAML extraction.staging override (not yet supported — "
                "     raise a backend ticket)."
            )

        else:
            recommendation = (
                f"{count} page(s) were rejected for reason {sub_reason!r}. "
                "No automated fix rule available."
            )

        results.append({
            "sub_reason": sub_reason,
            "count": count,
            "samples": samples,
            "severity": severity,
            "recommendation": recommendation,
        })

    # Sort highest count first.
    results.sort(key=lambda x: -x["count"])
    return results


def _common_url_path_prefix(urls: list[str]) -> str:
    """Return the longest common path prefix shared by all non-empty URLs.

    Only considers the path component (ignoring scheme/host).  Returns ``""``
    when there is no meaningful common prefix (fewer than 2 URLs or the shared
    prefix is just ``"/"``).
    """
    if len(urls) < 2:
        return ""
    paths: list[str] = []
    for u in urls:
        try:
            from urllib.parse import urlparse as _up
            p = _up(u).path.lower()
        except Exception:
            p = ""
        if p:
            paths.append(p)
    if not paths:
        return ""
    # Split into segments and find common prefix.
    split = [p.split("/") for p in paths]
    common: list[str] = []
    for segs in zip(*split):
        if len(set(segs)) == 1:
            common.append(segs[0])
        else:
            break
    prefix = "/".join(common)
    # Only return if it's more than just the root slash.
    return prefix if len(prefix) > 1 else ""
