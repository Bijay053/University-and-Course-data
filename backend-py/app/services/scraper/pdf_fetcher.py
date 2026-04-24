"""Download a remote PDF and extract its plain text.

Pure-Python via ``pypdf`` so no system deps (poppler etc.) are required.
Used by the university-level pipeline to read fee schedules and
admissions/policy PDFs whose URLs are stored in
``universities.scrape_config -> 'uniPages' -> 'feesPdf' / 'requirementsPdf'``.

Network is async (httpx); PDF parsing is CPU-bound but small (<5 MB)
so we run it on the calling loop without a thread offload.
"""
from __future__ import annotations

import io
import logging
from typing import Final

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

log = logging.getLogger(__name__)

_TIMEOUT_S: Final = 30.0
_MAX_BYTES: Final = 12 * 1024 * 1024  # 12 MB hard cap; bigger PDFs are catalogues, not fee schedules
_MAX_PAGES: Final = 80  # parse the first 80 pages — fee/policy PDFs are well below this
_USER_AGENT: Final = (
    "Mozilla/5.0 (compatible; UniversityScraper/1.0; +https://example.com/bot)"
)


async def download_pdf_text(url: str) -> str:
    """Fetch ``url`` and return the concatenated text of every page.

    Returns an empty string on any failure (HTTP error, non-PDF body,
    truncated/encrypted PDF, oversize). Never raises — callers treat
    "no text" the same as "no PDF configured".
    """
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("pdf fetch failed url=%s err=%s", url, exc)
        return ""

    body = resp.content or b""
    if not body:
        log.warning("pdf empty body url=%s", url)
        return ""
    if len(body) > _MAX_BYTES:
        log.warning("pdf too large url=%s bytes=%d", url, len(body))
        return ""

    # Some servers return HTML wrapping the PDF link instead of the raw bytes
    # (e.g. behind a "click to accept" page). Detect by magic bytes.
    if not body.lstrip().startswith(b"%PDF"):
        log.warning("pdf magic missing url=%s ctype=%s", url, resp.headers.get("content-type"))
        return ""

    try:
        reader = PdfReader(io.BytesIO(body))
    except PdfReadError as exc:
        log.warning("pdf parse failed url=%s err=%s", url, exc)
        return ""
    except Exception as exc:  # noqa: BLE001 — pypdf occasionally raises generic errors
        log.warning("pdf parse error url=%s err=%s", url, exc)
        return ""

    if getattr(reader, "is_encrypted", False):
        try:
            # Try empty password — many "encrypted" PDFs are just permission-locked.
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            log.warning("pdf encrypted url=%s", url)
            return ""

    pages = reader.pages[:_MAX_PAGES]
    chunks: list[str] = []
    for i, page in enumerate(pages):
        try:
            chunks.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001
            log.debug("pdf page %d text extract failed url=%s err=%s", i, url, exc)
            continue

    text = "\n".join(c for c in chunks if c).strip()
    log.info("pdf parsed url=%s pages=%d chars=%d", url, len(pages), len(text))
    return text
