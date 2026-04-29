"""Per-course vision-OCR fallback for image-only english requirement
tables (T208).

Some universities publish their English-language requirement matrix as a
PNG / SVG inside the course page rather than as machine-readable text —
the WhatUni / RMIT pattern. The HTTP and browser passes both return HTML
without extractable scores, the fee/IELTS extractors find nothing, and
the row stages with empty english slots that block auto-publish.

Mirrors Node's ``perCourseVisionFallback`` (routes/scrape.ts:11790):
1. Take the rendered HTML from the per-course browser pass.
2. Parse out every ``<img>`` tag, drop decorative assets (logo, icon,
   banner, hero, sprite), and keep at most :data:`_MAX_IMAGES` candidates.
3. Download each candidate, send the bytes + the english-requirements
   prompt to Gemini Vision.
4. Parse the resulting plain-text dump back through
   :func:`english_test.extract` and merge any new slot values.

Activation gate: ``GEMINI_API_KEY`` must be set AND at least one of
IELTS/PTE/TOEFL/CAE must still be empty after the browser pass. Without
both we no-op so the scrape stays cheap.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Final
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.services.ai import gemini_client
from app.services.scraper.extractors import english_test
from app.services.scraper.extractors.base import ExtractionResult

log = logging.getLogger(__name__)

# Overall-only slots — used for the "is anything still missing?" gate
# (we won't pay for vision when every overall is filled) and for the
# early-stop loop check inside the candidate-image walk. Sub-bands are
# excluded from this set on purpose: a course page that only fills
# `*_overall` should still trigger vision so we can recover sub-bands
# from the image, and conversely we shouldn't keep OCR'ing extra images
# just to backfill sub-bands once every overall is known.
_ENGLISH_OVERALL_SLOTS: Final = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)

# Output-filter slots — superset of overall + sub-bands. Vision results
# matching any of these keys are persisted into the merged payload and
# evidence rows. Without sub-bands here we used to silently drop
# `ielts_listening`, etc. even when the english_test extractor parsed
# them out of the Gemini response, leaving sub-bands stuck on the
# uni-wide PDF fallback (ASA: every course showed sub-bands = 5.5 even
# when the course-page MaSTER.png clearly says 6.0 across the board).
_ENGLISH_OUTPUT_SLOTS: Final = (
    *_ENGLISH_OVERALL_SLOTS,
    "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking",
    "pte_listening", "pte_reading", "pte_writing", "pte_speaking",
    "toefl_listening", "toefl_reading", "toefl_writing", "toefl_speaking",
)

# Backwards-compat alias for any external import / test that referenced
# the old name. New code should use one of the two pairs above.
_ENGLISH_SLOTS: Final = _ENGLISH_OVERALL_SLOTS

# Pages that wrap the requirements table in an image often have it as
# the *only* substantial graphic — we cap at 6 to avoid burning the
# Gemini budget on hero / banner / sponsor logos that slipped through
# the decorative filter. Node uses 8; 6 is a safer default for the
# Gemini-only Python build.
_MAX_IMAGES: Final = 6
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?>", re.IGNORECASE)
_SRC_RE = re.compile(r"\bsrc\s*=\s*\"([^\"]+)\"|\bsrc\s*=\s*'([^']+)'", re.IGNORECASE)
# Lazy-loading attributes used by modern sites — tried in order after src.
# data-src is most common (Intersection Observer pattern); others are
# plugin-specific (lazysizes → data-srcset, WP lazy → data-lazy,
# Cloudflare/Shopify → data-original, generic → data-lazy-src).
_LAZY_SRC_ATTRS: Final = (
    "data-src",
    "data-lazy-src",
    "data-lazy",
    "data-original",
)
_LAZY_SRC_RE: Final = re.compile(
    r'\b(?:' + '|'.join(re.escape(a) for a in _LAZY_SRC_ATTRS) + r')\s*=\s*"([^"]+)"|'
    r'\b(?:' + '|'.join(re.escape(a) for a in _LAZY_SRC_ATTRS) + r")\s*=\s*'([^']+)'",
    re.IGNORECASE,
)
_ALT_RE = re.compile(r"\balt\s*=\s*\"([^\"]*)\"|\balt\s*=\s*'([^']*)'", re.IGNORECASE)

# Words that flag an image as decorative — same allow-list as Node's
# ``isDecorativeImage``. Whole-word match against the URL path tail or
# the alt attribute (lower-cased).
_DECORATIVE_HINTS: Final = (
    "logo", "icon", "banner", "hero", "sprite", "avatar", "favicon",
    "social", "footer", "header", "nav", "menu", "decoration",
    "spinner", "loader", "placeholder", "thumb", "thumbnail",
    "sponsor", "partner", "facebook", "twitter", "instagram",
    "linkedin", "youtube", "tiktok",
    # Contact / UI widgets — never contain requirements tables
    "phone", "email", "map-marker", "map_marker", "marker",
    # ASA-style square-format logos ("ASA square 32.png")
    "square",
)

# URL path-segment keywords that strongly suggest the image contains
# English language requirement scores.  Images whose URL (lower-cased)
# contains any of these move to the FRONT of the processing queue so
# the early-stop condition triggers sooner and avoids paying for OCR on
# decorative images that slipped through the basic filter above.
_ENGLISH_IMG_PRIORITY_KEYWORDS: Final = (
    "english", "master", "bachelor", "undergraduate", "postgraduate",
    "ielts", "pte", "toefl", "cambridge", "duolingo", "language",
    "requirement", "admission", "entry",
)

_VISION_PROMPT: Final = (
    "You are reading an image taken from a university course page. The "
    "image likely contains an English-language requirements table or "
    "an admissions chart. Extract every English-test score VISIBLE IN "
    "THE IMAGE and return ONLY a plain-text dump with one fact per "
    "line, following this format exactly:\n"
    "  IELTS overall: [number]\n"
    "  IELTS listening: [number]\n"
    "  PTE overall: [number]\n"
    "  TOEFL iBT: [number]\n"
    "  Cambridge Advanced: [number]\n"
    "  Duolingo English Test: [number]\n"
    "CRITICAL RULES:\n"
    "- Only report scores you can literally READ from the image pixels.\n"
    "- Do NOT invent, guess, or recall typical values — ONLY transcribe "
    "what is visually present.\n"
    "- If a score is not shown, omit that line entirely.\n"
    "- If the image is decorative (logo, banner, icon, photo) with no "
    "numeric scores, return nothing at all.\n"
    "- Do NOT add commentary, headings, or markdown."
)


# Regex that matches English / Entry Requirements section headings.
# Used by :func:`_find_english_section_images` to locate images that are
# definitionally inside the requirements section — regardless of filename.
ENGLISH_SECTION_HEADING_RE: Final = re.compile(
    r"(?:English\s+(?:Language\s+)?Requirements?|Entry\s+Requirements?)",
    re.IGNORECASE,
)

# At least one recognised English-test name must appear in the OCR output
# for the result to be accepted. Guards against images inside the English
# section that are actually diagrams, logos, or "How to apply" graphics —
# Gemini may return text for them but none will mention IELTS/PTE/etc.
_VALID_ENGLISH_OCR_RE: Final = re.compile(
    r"\b(?:IELTS|PTE|TOEFL|Cambridge|Duolingo)\b",
    re.IGNORECASE,
)


def _find_english_section_images(html: str, base_url: str) -> list[tuple[str, str]]:
    """Find ``<img>`` tags inside a DOM section headed by English/Entry Requirements.

    Filename-agnostic — works for images with opaque CDN names such as
    ``Screenshot%202026-01-19%20104316.png`` (the ASAHE Bachelor image) that
    contain no level or English-requirement hint in the URL.

    Strategy: use BeautifulSoup to find every text node that matches
    :data:`ENGLISH_SECTION_HEADING_RE`, walk up to the nearest block
    container (``<section>``, ``<article>``, ``<div>``), then collect every
    ``<img>`` inside that container.  Lazy-loading attributes
    (``data-src`` / ``data-lazy-src`` / ``data-lazy`` / ``data-original``)
    are tried in order when ``src`` is absent or a data-URI.

    Returns ``[(absolute_url, alt_text)]`` deduplicated in DOM order.
    The caller (:func:`_extract_img_candidates`) promotes these to tier-0
    so they are processed before all other candidates.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:  # noqa: BLE001
        return result

    for text_node in soup.find_all(string=ENGLISH_SECTION_HEADING_RE):
        container = text_node.find_parent(["section", "article", "div"])
        if not container:
            continue
        for img in container.find_all("img"):
            src: str = img.get("src") or ""
            if not src or src.startswith("data:"):
                for attr in ("data-src", "data-lazy-src", "data-lazy", "data-original"):
                    src = img.get(attr) or ""
                    if src and not src.startswith("data:"):
                        break
            if not src or src.startswith("data:"):
                continue
            try:
                absolute = urljoin(base_url, src)
            except Exception:  # noqa: BLE001
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            alt: str = img.get("alt") or ""
            result.append((absolute, alt))

    return result


def _is_valid_english_ocr_result(ocr_text: str) -> bool:
    """Return ``True`` if OCR output contains at least one English-test keyword.

    Discards false-positive OCR runs where Gemini reads a logo, diagram, or
    "How to apply" graphic inside the English requirements section and returns
    generic text that happens to match the score format patterns but contains
    no IELTS / PTE / TOEFL / Cambridge / Duolingo mention.
    """
    return bool(ocr_text and _VALID_ENGLISH_OCR_RE.search(ocr_text))


def _extract_img_candidates(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return ``[(absolute_url, alt_text)]`` for non-decorative ``<img>``.

    Three priority tiers (lower number = higher priority):

    * **Tier 0** — images found by :func:`_find_english_section_images` inside
      a DOM section headed "English Requirements" / "Entry Requirements".
      Filename-agnostic: even opaque CDN names like a screenshot timestamp
      are included here if they live in the right section.
    * **Tier 1** — images whose URL contains a keyword from
      ``_ENGLISH_IMG_PRIORITY_KEYWORDS`` (``master``, ``bachelor``,
      ``ielts``, ``requirement``, etc.).
    * **Tier 2** — all other non-decorative images.

    The early-stop loop in :func:`maybe_vision_refetch` fires as soon as
    every overall slot is filled, so images processed later (tier 1/2) are
    only OCR'd if the tier-0 image didn't satisfy all overalls.

    The raw list is capped at ``_MAX_IMAGES`` after sorting.

    Lazy-loading support: modern sites use ``data-src`` / ``data-lazy-src``
    / ``data-lazy`` / ``data-original`` instead of ``src`` (Intersection
    Observer pattern).  The ``src`` attribute on those tags is either absent
    or a 1×1 transparent GIF placeholder — useless for OCR.  We now try
    ``src`` first; if it is missing or a data-URI we fall back to the lazy
    attributes in ``_LAZY_SRC_RE`` order so the real image URL is found.
    """
    # ── Tier 0: DOM-based English-section images ───────────────────────────
    # Build a set of absolute URLs for these so we can de-duplicate them out
    # of the regex scan below (they'd otherwise appear at tier 1 or 2 too).
    tier0 = _find_english_section_images(html, base_url)
    tier0_urls = {item[0] for item in tier0}

    # ── Tiers 1 & 2: regex scan of raw <img> tags ─────────────────────────
    raw: list[tuple[str, str]] = []
    for tag in _IMG_TAG_RE.findall(html or ""):
        m_src = _SRC_RE.search(tag)
        src = (m_src.group(1) or m_src.group(2) or "").strip() if m_src else ""
        # Lazy-load fallback: data-src / data-lazy-src / data-lazy / data-original
        if not src or src.startswith("data:"):
            m_lazy = _LAZY_SRC_RE.search(tag)
            if m_lazy:
                src = (m_lazy.group(1) or m_lazy.group(2) or "").strip()
        if not src or src.startswith("data:"):
            continue
        m_alt = _ALT_RE.search(tag)
        alt = (m_alt.group(1) or m_alt.group(2) or "").strip() if m_alt else ""
        # URL-decode before the word-boundary check so "%20square%20" is
        # seen as " square " (word boundaries intact).
        decision_text = f"{unquote(src)} {alt}".lower()
        if any(re.search(rf"\b{re.escape(h)}\b", decision_text) for h in _DECORATIVE_HINTS):
            continue
        try:
            absolute = urljoin(base_url, src)
        except Exception:  # noqa: BLE001 — never fail the scrape on a bad src
            continue
        # Skip images already captured as tier-0 English-section candidates.
        if absolute in tier0_urls:
            continue
        raw.append((absolute, alt))
        if len(raw) >= _MAX_IMAGES * 2:
            break

    # Promote English-requirement images to tier 1 (keyword in URL).
    def _priority(item: tuple[str, str]) -> int:
        url_lower = item[0].lower()
        return 1 if any(kw in url_lower for kw in _ENGLISH_IMG_PRIORITY_KEYWORDS) else 2

    raw.sort(key=_priority)

    # Combine: tier-0 first, then sorted tier-1/2, capped at _MAX_IMAGES.
    combined = tier0 + raw
    return combined[:_MAX_IMAGES]


async def _download(url: str) -> bytes | None:
    """Best-effort image download. ``None`` on any failure (timeout,
    404, oversized payload).
    """
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True
        ) as client:
            resp = await client.get(url)
        if resp.status_code >= 400 or not resp.content:
            return None
        # Cap at 4 MB — anything bigger is almost certainly a hero
        # image, not a requirements table; we don't want to drop a
        # 50 MB transparent PNG into the Gemini request.
        if len(resp.content) > 4_000_000:
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001
        log.debug("per_course_vision download %s failed: %s", url, exc)
        return None


VisionImageCache = dict[str, "asyncio.Future[dict[str, Any]]"]
"""Type alias for the per-scrape-run cache used by
:func:`maybe_vision_refetch`.

Stores ``asyncio.Future`` values (not raw parsed dicts) so that
concurrent coroutines processing the same image URL coalesce into a
single Gemini call — the leader resolves the future, waiters await it.
The orchestrator creates a fresh empty dict per scrape run; callers
should never read from the cache themselves, only pass it through.
Use the :func:`new_vision_image_cache` factory below to construct one
without depending on the internal value type.
"""


def new_vision_image_cache() -> VisionImageCache:
    """Construct a fresh empty per-scrape-run image cache.

    Provided so callers can stay decoupled from the internal Future-based
    representation: see :data:`VisionImageCache` for why we store futures
    instead of plain dicts.
    """
    return {}


async def maybe_vision_refetch(
    url: str,
    rendered_html: str | None,
    payload: dict[str, Any],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
    image_cache: VisionImageCache | None = None,
    degree_level: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Scan ``<img>`` tags on the course page and ask Gemini Vision to
    read any English-requirement tables found.

    **Phase A behaviour (Priority 2):** vision now runs whenever the
    page contains candidate images — even when every English slot is
    already filled from text extraction.  This ensures course-page image
    values (tier-4 authority) can override wrong tier-3 text values
    (e.g. ASAHE Masters whose true requirements are image-only).

    ``image_cache`` is an optional caller-owned mapping (one per scrape
    run, created via :func:`new_vision_image_cache`) keyed by absolute
    image URL. When the same image appears on multiple course pages
    (ASA's ``MaSTER.png`` lives on every Master page) we OCR it exactly
    once and reuse the parsed values — saving Gemini cost AND eliminating
    per-course vision non-determinism.  The cache stores
    ``asyncio.Future`` values so concurrent coroutines for the same URL
    coalesce: the first ("leader") performs the work, others ("waiters")
    await its result.

    ``degree_level`` is used for level-aware image selection: images
    whose filename matches the course's level (e.g. "master" in
    MaSTER.png for a Master's course) are promoted to the front of the
    processing queue so the correct level image is OCR'd first.

    Returns ``(filled_values, evidence_rows)``.  ``filled_values``
    contains ALL English slots the vision pass found — including slots
    that were already set from text extraction — so the caller can apply
    the authority model and decide whether to override.  Both dicts are
    empty when vision no-ops (no API key, no HTML, no candidate images,
    or Gemini skipped for every candidate).
    """
    if not rendered_html:
        log.info("[VISION SKIP] no rendered HTML — %s", url)
        return {}, []
    if not getattr(settings, "gemini_api_key", None):
        log.warning(
            "[VISION SKIP] GEMINI_API_KEY not configured — vision OCR will "
            "never run until the key is set. url=%s", url
        )
        return {}, []

    candidates = _extract_img_candidates(rendered_html, url)
    if not candidates:
        log.info("[VISION SKIP] no candidate images found on page — %s", url)
        return {}, []

    # ── Level-aware image promotion ───────────────────────────────────────
    # When the page has images for different degree levels (e.g. ASAHE has
    # MaSTER.png and BACHELOR.png), put the image that matches this course's
    # degree level at the front so the early-stop loop picks the right one
    # without burning Gemini calls on the wrong-level image.
    if degree_level:
        _dl = degree_level.lower()
        def _level_key(item: tuple[str, str]) -> int:
            _fn = item[0].lower().split("/")[-1]
            if "master" in _dl and "master" in _fn:
                return 0
            if ("bachelor" in _dl or "undergraduate" in _dl) and (
                "bachelor" in _fn or "undergrad" in _fn
            ):
                return 0
            if ("doctoral" in _dl or "phd" in _dl or "doctorate" in _dl) and (
                "phd" in _fn or "doctor" in _fn
            ):
                return 0
            if "diploma" in _dl and "diploma" in _fn:
                return 0
            if "certificate" in _dl and ("cert" in _fn or "certificate" in _fn):
                return 0
            return 1  # no level match — process after level-matched images
        candidates = sorted(candidates, key=_level_key)

    log.info(
        "[VISION] %d candidate image(s) found — starting OCR pass for %s",
        len(candidates), url,
    )
    if emit:
        await emit(
            "status",
            f"[VISION] {len(candidates)} candidate image(s) found — starting OCR pass",
            phase="fallback",
            kind="per_course_vision_start",
            url=url,
            candidates=len(candidates),
        )

    filled: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    images_consumed = 0
    cache_hits = 0
    for img_url, alt in candidates:
        # Stop early once the vision pass ITSELF has found every overall
        # slot — saves Gemini calls on extra images once the requirements
        # image has already been read.
        # NOTE: we do NOT check payload.get(k) here — payload values may
        # have come from text extraction (tier 3) and vision (tier 4) is
        # allowed to override them. Stopping on payload values would skip
        # the image entirely when text extraction filled the slot with the
        # wrong value (the ASAHE bug).
        if all(k in filled for k in _ENGLISH_OVERALL_SLOTS):
            break

        # ── Per-image cache lookup with in-flight coalescing ─────────
        # Same image URL seen on a sibling course in this scrape? Reuse
        # whatever english_test parsed out of it last time. This is
        # what makes the 4 ASA Masters internally consistent (all see
        # the same MaSTER.png) without re-paying Gemini for each one.
        #
        # The cache value is an asyncio.Future, NOT the parsed dict
        # directly, so that when N coroutines (e.g. all 4 ASA IT
        # Masters running under _MAX_PARALLEL_FETCH=4) reach this point
        # for the same img_url, only the first one ("leader") performs
        # the download + Gemini call + parse, and the others ("waiters")
        # await the leader's result. Without the Future, all 4 would
        # see "url not in cache", all 4 would race to fire Gemini, and
        # the cache would only help cross-WAVE (later courses) — not
        # the very ASA-Masters scenario the cache was built for.
        normalized: dict[str, Any] | None = None
        cached_method = "per_course_vision"
        leader_future: asyncio.Future[dict[str, Any]] | None = None
        if image_cache is not None:
            existing: asyncio.Future[dict[str, Any]] | None = image_cache.get(img_url)
            if existing is None:
                # Be the leader for this URL. Install our Future
                # synchronously (no await between get and set) so any
                # subsequent coroutine in the same event-loop iteration
                # sees it and becomes a waiter.
                leader_future = asyncio.get_running_loop().create_future()
                image_cache[img_url] = leader_future
            else:
                # Waiter path: someone is already (or has already) OCR'd
                # this image. Await the Future (resolves instantly if
                # already set) and treat the result as a cache hit.
                try:
                    normalized = await existing
                except Exception:  # noqa: BLE001 — leader's error already logged
                    normalized = {}
                cache_hits += 1
                cached_method = "per_course_vision_cached"

        if leader_future is not None or image_cache is None:
            # Leader (or no cache at all) actually does the work.
            try:
                log.info("[VISION] attempting OCR on %s", img_url)
                img_bytes = await _download(img_url)
                if not img_bytes:
                    # Negative-cache: a 404 / oversized image must not
                    # be re-downloaded per sibling course. Resolve the
                    # Future with {} so waiters short-circuit too.
                    log.info("[VISION FAIL] %s: download returned empty (404 or oversized)", img_url)
                    if leader_future is not None:
                        leader_future.set_result({})
                    continue
                images_consumed += 1
                try:
                    resp = await gemini_client.generate_with_images(
                        _VISION_PROMPT, [img_bytes]
                    )
                except Exception as exc:  # noqa: BLE001
                    _fail_msg = (
                        f"[VISION FAIL] {img_url[:70]}: Gemini call failed — {exc} "
                        f"(quota exhausted or API key invalid?)"
                    )
                    log.warning(_fail_msg)
                    if emit:
                        await emit(
                            "status",
                            _fail_msg,
                            phase="fallback",
                            kind="per_course_vision_fail",
                            url=url,
                            image_url=img_url,
                        )
                    # Evict this URL from the image cache so sibling courses
                    # can retry independently instead of inheriting the failure.
                    # (Download failures stay cached because a 404 won't fix
                    # itself; API quota failures may clear between courses.)
                    if image_cache is not None and img_url in image_cache:
                        del image_cache[img_url]
                    if leader_future is not None and not leader_future.done():
                        leader_future.set_result({})
                    continue
                if resp.skipped or not resp.text:
                    skip_reason = getattr(resp, "skip_reason", "no text returned")
                    _skip_msg = (
                        f"[VISION FAIL] {img_url[:70]}: Gemini skipped — {skip_reason} "
                        f"(likely quota exhausted)"
                    )
                    log.warning(_skip_msg)
                    if emit:
                        await emit(
                            "status",
                            _skip_msg,
                            phase="fallback",
                            kind="per_course_vision_fail",
                            url=url,
                            image_url=img_url,
                        )
                    if image_cache is not None and img_url in image_cache:
                        del image_cache[img_url]
                    if leader_future is not None and not leader_future.done():
                        leader_future.set_result({})
                    continue
                # Validate that the OCR text contains at least one English-test
                # keyword (IELTS, PTE, TOEFL, Cambridge, Duolingo). This guards
                # against images that are inside the English requirements section
                # of the page but are actually logos, diagrams, or "How to apply"
                # graphics — Gemini returns text for them but none of it refers to
                # a language test, so the result is useless and should be discarded
                # before we attempt to parse scores from it.
                if not _is_valid_english_ocr_result(resp.text):
                    log.info(
                        "[VISION SKIP OCR] %s: Gemini returned text but no "
                        "IELTS/PTE/TOEFL/Cambridge/Duolingo keyword found — "
                        "likely a non-requirements image (logo, diagram, etc.)",
                        img_url,
                    )
                    if leader_future is not None:
                        leader_future.set_result({})
                    continue
                # Wrap the plain-text dump back in a tiny HTML shell so the
                # existing english_test extractor can re-parse it. The
                # extractor walks <p>/<li>-like text — pre-tags work too
                # because the underlying _text helper strips tags.
                text_html = "<pre>" + resp.text + "</pre>"
                try:
                    results: list[ExtractionResult] = await english_test.extract(
                        text_html, url
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("[VISION FAIL] %s: english_test parse failed — %s", img_url, exc)
                    if leader_future is not None:
                        leader_future.set_result({})
                    continue
                normalized = {}
                for r in results:
                    if not r.normalized:
                        continue
                    for k, v in r.normalized.items():
                        if v in (None, "", 0):
                            continue
                        if k not in _ENGLISH_OUTPUT_SLOTS:
                            continue
                        normalized.setdefault(k, v)
                if normalized:
                    log.info(
                        "[VISION OK] %s: extracted %s",
                        img_url,
                        " ".join(f"{k}={v}" for k, v in sorted(normalized.items())),
                    )
                else:
                    log.info("[VISION] %s: Gemini returned text but no English scores parsed", img_url)
                if leader_future is not None:
                    leader_future.set_result(dict(normalized))
            except BaseException as exc:
                # Propagate the leader failure to any waiters so they
                # don't await forever. Re-raise so existing exception
                # handling (the outer try/except in extract_course)
                # behaves identically to before.
                if leader_future is not None and not leader_future.done():
                    leader_future.set_exception(exc)
                raise

        if not normalized:
            continue

        for k, v in normalized.items():
            if k not in _ENGLISH_OUTPUT_SLOTS:
                continue
            if v in (None, "", 0):
                continue
            # `filled` de-dupes within one vision pass (first image wins
            # for any given slot). We do NOT gate on `payload.get(k)` here
            # — the caller applies the authority model (tier-4 vision may
            # override tier-3 text) and decides whether to write to payload.
            if k in filled:
                continue
            filled[k] = v
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.85,
                    "method": cached_method,
                    # source_url is the image URL, not the course page URL,
                    # so the Evidence Review panel can show exactly which
                    # image the value came from.
                    "source_url": img_url,
                    "snippet": (alt or img_url)[:240],
                }
            )

    if emit:
        def _fmt(k: str) -> str:
            v = filled.get(k)
            return str(v) if v not in (None, "", 0) else "—"

        cache_note = f" (cache hits {cache_hits})" if cache_hits else ""
        await emit(
            "status",
            f"[per-course vision img {images_consumed}/{len(candidates)}{cache_note}] "
            f"{url} — IELTS={_fmt('ielts_overall')} "
            f"PTE={_fmt('pte_overall')} TOEFL={_fmt('toefl_overall')} "
            f"CAE={_fmt('cambridge_overall')}",
            phase="fallback",
            kind="per_course_vision_done",
            url=url,
            consumed=images_consumed,
            cache_hits=cache_hits,
            filled=list(filled.keys()),
        )

    return filled, evidence
