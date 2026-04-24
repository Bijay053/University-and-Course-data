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
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.services.ai import gemini_client
from app.services.scraper.extractors import english_test
from app.services.scraper.extractors.base import ExtractionResult

log = logging.getLogger(__name__)

_ENGLISH_SLOTS: Final = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
)

# Pages that wrap the requirements table in an image often have it as
# the *only* substantial graphic — we cap at 6 to avoid burning the
# Gemini budget on hero / banner / sponsor logos that slipped through
# the decorative filter. Node uses 8; 6 is a safer default for the
# Gemini-only Python build.
_MAX_IMAGES: Final = 6
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?>", re.IGNORECASE)
_SRC_RE = re.compile(r"\bsrc\s*=\s*\"([^\"]+)\"|\bsrc\s*=\s*'([^']+)'", re.IGNORECASE)
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
)

_VISION_PROMPT: Final = (
    "You are reading an image taken from a university course page. The "
    "image likely contains an English-language requirements table or "
    "an admissions chart. Extract every English-test score visible in "
    "the image and return ONLY a plain-text dump with one fact per "
    "line, like:\n"
    "  IELTS overall: 6.5\n"
    "  IELTS listening: 6.0\n"
    "  PTE overall: 58\n"
    "  TOEFL iBT: 79\n"
    "  Cambridge Advanced: 176\n"
    "  Duolingo English Test: 105\n"
    "Include the score exactly as shown. Do NOT add commentary, "
    "headings, or markdown. If a value is not present, omit the line "
    "— never guess. If the image is decorative (logo/banner/icon) "
    "with no scores, return nothing."
)


def _extract_img_candidates(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return ``[(absolute_url, alt_text)]`` for non-decorative ``<img>``.

    Order is preserved (top-of-document first) and capped at
    :data:`_MAX_IMAGES` so the caller never accidentally fans out into
    a 100-image hero gallery.
    """
    out: list[tuple[str, str]] = []
    for tag in _IMG_TAG_RE.findall(html or ""):
        m_src = _SRC_RE.search(tag)
        if not m_src:
            continue
        src = (m_src.group(1) or m_src.group(2) or "").strip()
        if not src or src.startswith("data:"):
            continue
        m_alt = _ALT_RE.search(tag)
        alt = (m_alt.group(1) or m_alt.group(2) or "").strip() if m_alt else ""
        decision_text = f"{src} {alt}".lower()
        if any(re.search(rf"\b{re.escape(h)}\b", decision_text) for h in _DECORATIVE_HINTS):
            continue
        try:
            absolute = urljoin(base_url, src)
        except Exception:  # noqa: BLE001 — never fail the scrape on a bad src
            continue
        out.append((absolute, alt))
        if len(out) >= _MAX_IMAGES:
            break
    return out


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


async def maybe_vision_refetch(
    url: str,
    rendered_html: str | None,
    payload: dict[str, Any],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """If at least one english slot is still missing AND vision is
    available, scan ``<img>`` tags on the rendered HTML and ask Gemini
    Vision to read the score table.

    Returns ``(filled_values, evidence_rows)`` — both empty when the
    fallback no-ops (slots already filled, no API key, no rendered HTML,
    no candidate images, or Gemini skipped).
    """
    if not rendered_html:
        return {}, []
    if not getattr(settings, "gemini_api_key", None):
        return {}, []
    if not any(payload.get(k) in (None, "", 0) for k in _ENGLISH_SLOTS):
        return {}, []

    candidates = _extract_img_candidates(rendered_html, url)
    if not candidates:
        return {}, []

    if emit:
        await emit(
            "status",
            f"[FALLBACK] [per-course vision img 0/{len(candidates)}] {url}",
            phase="fallback",
            kind="per_course_vision_start",
            url=url,
            candidates=len(candidates),
        )

    filled: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    images_consumed = 0
    for img_url, alt in candidates:
        # Stop early once every slot is filled — saves Gemini calls and
        # keeps the live log tidy on pages where the first image was the
        # one we needed.
        if all(payload.get(k) not in (None, "", 0) or k in filled for k in _ENGLISH_SLOTS):
            break
        img_bytes = await _download(img_url)
        if not img_bytes:
            continue
        images_consumed += 1
        try:
            resp = await gemini_client.generate_with_images(
                _VISION_PROMPT, [img_bytes]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("per_course_vision Gemini failed for %s: %s", img_url, exc)
            continue
        if resp.skipped or not resp.text:
            if resp.skipped:
                log.info(
                    "per_course_vision: Gemini skipped (%s) for %s",
                    resp.skip_reason,
                    img_url,
                )
            continue
        # Wrap the plain-text dump back in a tiny HTML shell so the
        # existing english_test extractor can re-parse it. The extractor
        # walks <p>/<li>-like text — pre-tags work too because the
        # underlying _text helper strips tags.
        text_html = "<pre>" + resp.text + "</pre>"
        try:
            results: list[ExtractionResult] = await english_test.extract(
                text_html, url
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("english_test parse-of-vision failed: %s", exc)
            continue
        for r in results:
            if not r.normalized:
                continue
            for k, v in r.normalized.items():
                if k not in _ENGLISH_SLOTS:
                    continue
                if v in (None, "", 0):
                    continue
                if k in filled or payload.get(k) not in (None, "", 0):
                    continue
                filled[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": min(1.0, (r.confidence or 0.4) + 0.05),
                        "method": "per_course_vision",
                        "snippet": (alt or img_url)[:240],
                    }
                )

    if emit:
        def _fmt(k: str) -> str:
            v = filled.get(k)
            return str(v) if v not in (None, "", 0) else "—"

        await emit(
            "status",
            f"[FALLBACK] [per-course vision img {images_consumed}/{len(candidates)}] "
            f"{url} — IELTS={_fmt('ielts_overall')} "
            f"PTE={_fmt('pte_overall')} TOEFL={_fmt('toefl_overall')} "
            f"CAE={_fmt('cambridge_overall')}",
            phase="fallback",
            kind="per_course_vision_done",
            url=url,
            consumed=images_consumed,
            filled=list(filled.keys()),
        )

    return filled, evidence
