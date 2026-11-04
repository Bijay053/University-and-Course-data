"""Isolated, bounded text-only PDF parsing; no network, OCR or generated values.

Run as a child process so cancelling research can actually stop hostile parsing.
Uses the same pypdf text parser as scraper/pdf_fetcher, but never its downloader
(that downloader follows redirects and has no DNS pinning).
"""
from __future__ import annotations

import io
import json
import resource
import sys

MAX_PAGES = 12
MAX_TEXT = 48_000
MAX_PAGE_TEXT = 12_000
MAX_BYTES = 2_000_000


class PdfLimitError(ValueError):
    """Only these locally authored messages may leave the parser."""


def extract_pages(raw: bytes) -> dict:
    from pypdf import PdfReader

    if len(raw) > MAX_BYTES or not raw.lstrip().startswith(b"%PDF"):
        raise PdfLimitError("PDF exceeds the byte limit or has an invalid signature.")
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted and not reader.decrypt(""):
        raise PdfLimitError("PDF is password protected.")
    count = len(reader.pages)
    if count > MAX_PAGES:
        raise PdfLimitError(f"PDF exceeds the {MAX_PAGES}-page research limit.")
    pages, unreadable, size = [], [], 0
    for number, page in enumerate(reader.pages, 1):
        # Keep table spacing and page boundaries; do not turn cells into HTML
        # and send them through extractors which assume HTML audience scopes.
        text = (page.extract_text(extraction_mode="layout") or "").strip() if "/Contents" in page else ""
        size += len(text)
        if len(text) > MAX_PAGE_TEXT or size > MAX_TEXT:
            raise PdfLimitError("PDF exceeds the extracted-text research limit.")
        if len("".join(text.split())) < 40:
            unreadable.append(number)
        else:
            pages.append({"page": number, "text": text})
    if not pages:
        raise PdfLimitError("PDF has no readable text; image-only pages were not OCR-verified.")
    return {"pages": pages, "pageCount": count, "unreadablePages": unreadable}


def main():
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    try:
        result = extract_pages(sys.stdin.buffer.read(MAX_BYTES + 1))
    except PdfLimitError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "PDF is malformed or cannot be read within the parsing limits."}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
