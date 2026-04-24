"""Vision-OCR fallback for PDFs that ``pypdf`` cannot extract text from.

Many universities publish their fee schedules and admissions/IELTS
policies as scanned PDFs (image-only) or as PDFs whose text layer is
encoded so opaquely that ``pypdf`` returns garbage. The text-based
:func:`pdf_fetcher.download_pdf_text` path returns "" for these files
and the per-course fallback that depends on the result silently fills
nothing.

This module renders the first few PDF pages to JPEG with ``pypdfium2``
(pure-binary wheel, no system deps) and asks Gemini Vision to extract
the structured fee/IELTS values, then returns them as a flat plain-text
blob the existing fee/english_test extractors can re-run on.

Network/AI calls go through :mod:`app.services.ai.gemini_client` so the
daily Gemini budget continues to apply — when the budget is exhausted
this module returns ``""`` and the orchestrator carries on.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Final

from app.services.ai import gemini_client

log = logging.getLogger(__name__)

_MAX_PAGES: Final = 3  # fee schedules + IELTS pages live in the first few pages
_RENDER_DPI: Final = 144  # readable for vision OCR; bigger = bigger payload, no win
_JPEG_QUALITY: Final = 75
_VISION_PROMPT: Final = (
    "You are reading a university PDF (fee schedule or admissions/English "
    "language requirements). Extract every numeric fee and English-test "
    "score visible on these pages. Return a plain-text dump with one fact "
    "per line, like:\n"
    "  International tuition fee: AUD $34,000 per year\n"
    "  IELTS overall: 6.5\n"
    "  IELTS listening: 6.0\n"
    "  PTE overall: 58\n"
    "  TOEFL iBT: 79\n"
    "Include currency symbols and units exactly as shown. Do NOT add "
    "commentary, headings, or markdown. If a value is not present, omit "
    "the line — never guess. Output only the lines, no preamble."
)


def _render_pdf_to_jpegs(pdf_bytes: bytes, *, max_pages: int, dpi: int, quality: int) -> list[bytes]:
    """Synchronous PDF -> JPEG render. Runs in a thread via ``asyncio.to_thread``.

    pypdfium2 is the binding to PDFium (the same renderer Chrome uses).
    No external poppler/ImageMagick required.
    """
    import pypdfium2 as pdfium
    from PIL import Image

    out: list[bytes] = []
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        log.warning("pdf_vision: pypdfium open failed: %s", exc)
        return out

    try:
        n = min(len(pdf), max_pages)
        scale = dpi / 72.0  # PDF userspace is 72 DPI
        for i in range(n):
            page = pdf[i]
            try:
                bitmap = page.render(scale=scale)
                pil_img: Image.Image = bitmap.to_pil()
                # Convert to RGB so JPEG can encode it (PDF renders may be
                # RGBA/grayscale).
                if pil_img.mode != "RGB":
                    pil_img = pil_img.convert("RGB")
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=quality, optimize=True)
                out.append(buf.getvalue())
            except Exception as exc:
                log.warning("pdf_vision: render page %d failed: %s", i, exc)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    return out


async def extract_via_vision(
    pdf_bytes: bytes,
    *,
    max_pages: int = _MAX_PAGES,
    prompt: str = _VISION_PROMPT,
) -> str:
    """Render ``pdf_bytes`` to images and ask Gemini Vision for a text dump.

    Returns a plain-text blob (one fact per line, see ``_VISION_PROMPT``)
    that callers can wrap as HTML and feed back into the existing
    :mod:`extractors.fee` / :mod:`extractors.english_test` extractors.
    Returns ``""`` when no images could be rendered, when Gemini is
    skipped (no API key, budget exhausted), or when the model responds
    with empty text.

    Never raises — vision is a *fallback* path; failures must degrade to
    the no-vision case rather than abort the scrape.
    """
    if not pdf_bytes:
        return ""
    images = await asyncio.to_thread(
        _render_pdf_to_jpegs,
        pdf_bytes,
        max_pages=max_pages,
        dpi=_RENDER_DPI,
        quality=_JPEG_QUALITY,
    )
    if not images:
        return ""
    resp = await gemini_client.generate_with_images(prompt, images)
    if resp.skipped or not resp.text:
        if resp.skipped:
            log.info("pdf_vision: Gemini skipped (%s)", resp.skip_reason)
        return ""
    return resp.text
