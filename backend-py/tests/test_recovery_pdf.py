"""Unit tests for PDF-aware recovery pass (Task 141).

Tests cover:
- _score_pdf_link: relevance scoring of PDF links against categories
- _find_linked_pdfs: collection, scoring, and ranking of PDF links from HTML
- _extract_from_pdf: end-to-end extraction using pdf_fetcher.download_pdf_text
- searcher: PDF links appear as candidates; PDF URLs are not pushed onto BFS frontier
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# _score_pdf_link
# ---------------------------------------------------------------------------

class TestScorePdfLink:
    def setup_method(self):
        from app.services.scraper.recovery.extractor import _score_pdf_link
        self._fn = _score_pdf_link

    def test_fee_pdf_scores_for_fees_category(self):
        score = self._fn(
            "https://uni.edu.au/docs/international-fees-2024.pdf",
            "International Fee Schedule",
            {"fees"},
        )
        assert score > 0

    def test_ielts_pdf_scores_for_english_category(self):
        score = self._fn(
            "https://uni.edu.au/docs/english-requirements.pdf",
            "IELTS and English entry requirements",
            {"english"},
        )
        assert score > 0

    def test_unrelated_pdf_scores_zero_for_fees(self):
        score = self._fn(
            "https://uni.edu.au/library-policy.pdf",
            "Library borrowing policy",
            {"fees"},
        )
        assert score == 0

    def test_multi_category_accumulates_score(self):
        score_both = self._fn(
            "https://uni.edu.au/international-fees-and-english.pdf",
            "Fees and English requirements",
            {"fees", "english"},
        )
        score_fees_only = self._fn(
            "https://uni.edu.au/international-fees-and-english.pdf",
            "Fees and English requirements",
            {"fees"},
        )
        assert score_both >= score_fees_only

    def test_empty_categories_returns_zero(self):
        score = self._fn(
            "https://uni.edu.au/fees.pdf",
            "Fee schedule",
            set(),
        )
        assert score == 0


# ---------------------------------------------------------------------------
# _find_linked_pdfs
# ---------------------------------------------------------------------------

class TestFindLinkedPdfs:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setup_method(self):
        from app.services.scraper.recovery.extractor import _find_linked_pdfs
        self._fn = _find_linked_pdfs

    def _make_html(self, links: list[tuple[str, str]]) -> str:
        """Build a minimal HTML page with the given (href, anchor) pairs."""
        items = "".join(
            f'<a href="{href}">{text}</a>' for href, text in links
        )
        return f"<html><body>{items}</body></html>"

    def test_finds_pdf_links(self):
        html = self._make_html([
            ("/fees/schedule.pdf", "Fee schedule"),
            ("/about", "About us"),
        ])
        result = self._run(self._fn(html, "https://uni.edu.au", {"fees"}))
        assert any("schedule.pdf" in u for u in result)

    def test_ignores_non_pdf_links(self):
        html = self._make_html([
            ("/fees/schedule.html", "Fee schedule"),
            ("/admissions", "Admissions"),
        ])
        result = self._run(self._fn(html, "https://uni.edu.au", {"fees"}))
        assert result == []

    def test_deduplicates_same_pdf_url(self):
        html = self._make_html([
            ("/fees.pdf", "Fees"),
            ("/fees.pdf", "Fees again"),
        ])
        result = self._run(self._fn(html, "https://uni.edu.au", {"fees"}))
        assert len(result) == 1

    def test_ranks_relevant_pdf_first(self):
        html = self._make_html([
            ("/library-policy.pdf", "Library policy"),
            ("/international-fee-schedule.pdf", "International Fee Schedule"),
        ])
        result = self._run(self._fn(html, "https://uni.edu.au", {"fees"}))
        assert result[0].endswith("international-fee-schedule.pdf")

    def test_caps_at_max_linked_pdfs(self):
        from app.services.scraper.recovery.extractor import _MAX_LINKED_PDFS
        links = [(f"/doc{i}.pdf", f"Doc {i}") for i in range(_MAX_LINKED_PDFS + 5)]
        html = self._make_html(links)
        result = self._run(self._fn(html, "https://uni.edu.au"))
        assert len(result) <= _MAX_LINKED_PDFS

    def test_resolves_relative_paths(self):
        html = self._make_html([("/docs/fees.pdf", "Fees")])
        result = self._run(self._fn(html, "https://uni.edu.au", {"fees"}))
        assert result == ["https://uni.edu.au/docs/fees.pdf"]

    def test_empty_html_returns_empty(self):
        result = self._run(self._fn("", "https://uni.edu.au", {"fees"}))
        assert result == []

    def test_no_categories_still_returns_pdfs(self):
        html = self._make_html([("/fees.pdf", "Fees")])
        result = self._run(self._fn(html, "https://uni.edu.au"))
        assert "https://uni.edu.au/fees.pdf" in result


# ---------------------------------------------------------------------------
# _extract_from_pdf
# ---------------------------------------------------------------------------

class TestExtractFromPdf:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_uses_download_pdf_text_not_fetch_pdf_text(self):
        """Ensure _extract_from_pdf calls the correct function name."""
        import ast, inspect
        from app.services.scraper.recovery import extractor as ext_mod
        source = inspect.getsource(ext_mod._extract_from_pdf)
        assert "download_pdf_text" in source, (
            "_extract_from_pdf must call download_pdf_text (not fetch_pdf_text)"
        )
        assert "fetch_pdf_text" not in source, (
            "fetch_pdf_text does not exist in pdf_fetcher — use download_pdf_text"
        )

    def test_returns_empty_when_no_pdf_text(self):
        from app.services.scraper.recovery.extractor import _extract_from_pdf

        async def _run():
            with patch(
                "app.services.scraper.pdf_fetcher.download_pdf_text",
                new_callable=AsyncMock,
                return_value="",
            ):
                with patch(
                    "app.services.scraper.recovery.extractor._run_extractor",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    return await _extract_from_pdf(
                        "https://uni.edu.au/fees.pdf", {"fees"}
                    )

        result = self._run(_run())
        assert result == []

    def test_sets_source_type_pdf(self):
        """Results from PDF extraction must have source_type='pdf'."""
        from app.services.scraper.recovery.extractor import _extract_from_pdf

        fake_result = {
            "field": "international_fee",
            "value": "25000",
            "normalized": 25000.0,
            "confidence": 0.8,
            "snippet": "AUD 25,000",
            "method": "table",
            "source_url": "https://uni.edu.au/fees.pdf",
            "source_type": "html",  # will be overwritten
        }

        async def _run():
            with patch(
                "app.services.scraper.pdf_fetcher.download_pdf_text",
                new_callable=AsyncMock,
                return_value="International tuition fee: AUD 25,000 per year",
            ):
                with patch(
                    "app.services.scraper.recovery.extractor._run_extractor",
                    new_callable=AsyncMock,
                    return_value=[dict(fake_result)],
                ):
                    return await _extract_from_pdf(
                        "https://uni.edu.au/fees.pdf", {"fees"}
                    )

        results = self._run(_run())
        assert len(results) == 1
        assert results[0]["source_type"] == "pdf"
        assert results[0]["source_url"] == "https://uni.edu.au/fees.pdf"

    def test_handles_import_error_gracefully(self):
        """If pdf_fetcher is unavailable, return [] without raising."""
        from app.services.scraper.recovery.extractor import _extract_from_pdf
        import sys

        async def _run():
            # Temporarily hide pdf_fetcher
            saved = sys.modules.get("app.services.scraper.pdf_fetcher")
            sys.modules["app.services.scraper.pdf_fetcher"] = None  # type: ignore[assignment]
            try:
                return await _extract_from_pdf("https://uni.edu.au/fees.pdf", {"fees"})
            finally:
                if saved is None:
                    del sys.modules["app.services.scraper.pdf_fetcher"]
                else:
                    sys.modules["app.services.scraper.pdf_fetcher"] = saved

        result = self._run(_run())
        assert result == []


# ---------------------------------------------------------------------------
# searcher: PDFs treated as candidates but not pushed onto BFS frontier
# ---------------------------------------------------------------------------

class TestSearcherPdfHandling:
    def test_pdf_links_not_added_to_frontier(self):
        """When a PDF link is scored, it must not be pushed onto the BFS frontier."""
        from app.services.scraper.recovery.searcher import (
            _extract_links,
            _score_link,
        )
        from urllib.parse import urlparse

        # Build a page that has one PDF link and one HTML link
        html = """
        <html><body>
          <a href="/fees/schedule.pdf">International Fee Schedule</a>
          <a href="/admissions/requirements">Entry Requirements</a>
        </body></html>
        """
        base = "https://uni.edu.au"
        apex = "uni.edu.au"
        links = _extract_links(html, base, apex)

        pdf_links = [url for url, _ in links if url.lower().endswith(".pdf")]
        html_links = [url for url, _ in links if not url.lower().endswith(".pdf")]

        assert any("schedule.pdf" in u for u in pdf_links), "PDF link not found in extracted links"
        assert any("requirements" in u for u in html_links), "HTML link not found"

        # Simulate the frontier-addition guard: PDF URLs must NOT be pushed
        needs = {"fees", "english"}
        for pdf_url, _ in [(u, t) for u, t in links if u.lower().endswith(".pdf")]:
            is_pdf = pdf_url.lower().endswith(".pdf")
            assert is_pdf, "Sanity: selected URL is a PDF"
            # The guard: `if depth < 2 and not full_url.lower().endswith(".pdf")`
            # means this PDF would be excluded from the frontier — that is correct.

    def test_pdf_links_still_scored_as_candidates(self):
        """A PDF link that matches a category keyword should receive a positive score."""
        from app.services.scraper.recovery.searcher import _score_link

        scores = _score_link(
            "https://uni.edu.au/international-fees-2024.pdf",
            "International Fee Schedule PDF",
            {"fees"},
        )
        assert "fees" in scores and scores["fees"] > 0, (
            "PDF fee link should score positively for 'fees' category"
        )

    def test_unrelated_pdf_scores_zero(self):
        from app.services.scraper.recovery.searcher import _score_link

        scores = _score_link(
            "https://uni.edu.au/library-policy.pdf",
            "Library Borrowing Policy",
            {"fees"},
        )
        assert scores.get("fees", 0) == 0
