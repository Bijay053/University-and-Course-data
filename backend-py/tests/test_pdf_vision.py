"""Tests for the vision-OCR PDF fallback.

We don't drive the real Gemini API in tests — we patch the
``generate_with_images`` callable and assert two things that matter:

1. The vision module renders the supplied PDF bytes to ≥1 image and
   passes them through to the Gemini call (proving the wiring).
2. When Gemini is skipped (no key, budget exhausted), the function
   returns "" rather than raising, so the orchestrator can degrade.

PDF rendering uses the real ``pypdfium2`` library against a tiny in-memory
PDF generated with ``pypdf``; this catches dependency-import regressions.
"""
from __future__ import annotations

import io

import pytest
from pypdf import PdfReader, PdfWriter

from app.services.ai.gemini_client import GeminiResponse
from app.services.scraper import pdf_vision


def _tiny_pdf_bytes() -> bytes:
    """Build a 1-page PDF with no text content. The renderer still
    succeeds — we only need *something* a real PDF reader will accept."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    out = buf.getvalue()
    # Sanity: the bytes are a valid PDF that pypdf can re-read.
    PdfReader(io.BytesIO(out))
    return out


@pytest.mark.asyncio
async def test_extract_via_vision_renders_and_calls_gemini(monkeypatch):
    captured: dict = {}

    async def fake_generate_with_images(prompt, images, **kwargs):
        captured["prompt"] = prompt
        captured["images"] = images
        captured["kwargs"] = kwargs
        return GeminiResponse(
            text="IELTS overall: 6.5\nInternational tuition fee: AUD $30,000 per year",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.0001,
        )

    monkeypatch.setattr(
        "app.services.scraper.pdf_vision.gemini_client.generate_with_images",
        fake_generate_with_images,
    )

    out = await pdf_vision.extract_via_vision(_tiny_pdf_bytes())
    assert "IELTS overall" in out
    assert captured["images"], "at least one image should be passed to Gemini"
    assert all(isinstance(b, (bytes, bytearray)) for b in captured["images"])
    # JPEG magic bytes — confirms the renderer encoded as JPEG.
    assert captured["images"][0][:3] == b"\xff\xd8\xff"


@pytest.mark.asyncio
async def test_extract_via_vision_returns_empty_when_skipped(monkeypatch):
    async def fake_skipped(prompt, images, **kwargs):
        return GeminiResponse(
            text="",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            skipped=True,
            skip_reason="GEMINI_API_KEY not set",
        )

    monkeypatch.setattr(
        "app.services.scraper.pdf_vision.gemini_client.generate_with_images",
        fake_skipped,
    )
    out = await pdf_vision.extract_via_vision(_tiny_pdf_bytes())
    assert out == ""


@pytest.mark.asyncio
async def test_extract_via_vision_empty_input_returns_empty():
    assert await pdf_vision.extract_via_vision(b"") == ""


@pytest.mark.asyncio
async def test_extract_via_vision_unparseable_pdf_returns_empty(monkeypatch):
    """Garbage input → renderer fails open → empty string, no exception."""
    # No need to patch Gemini: with zero images, generate_with_images
    # would never be called. Patch anyway as a defensive check.
    async def fake(prompt, images, **kwargs):
        raise AssertionError("Gemini should not be called when no images render")

    monkeypatch.setattr(
        "app.services.scraper.pdf_vision.gemini_client.generate_with_images",
        fake,
    )
    out = await pdf_vision.extract_via_vision(b"not a pdf at all")
    assert out == ""
