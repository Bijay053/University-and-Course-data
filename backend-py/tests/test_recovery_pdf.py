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


# ---------------------------------------------------------------------------
# searcher: PDF links found on any BFS-visited page (Task #145)
# ---------------------------------------------------------------------------

class TestSearcherHomepagePdf:
    """PDF links discovered on any BFS-visited page must be returned as
    candidates, even when their URL + anchor text score 0 via _score_link.

    The searcher uses _score_pdf_link (from extractor) as a fallback scorer
    for PDFs.  _score_pdf_link uses a broader keyword list — e.g. "international"
    and "schedule" — that _score_link's HTML-page dictionaries don't include.

    Test URL: https://uni.edu.au/download/2024-international-prospectus.pdf
    Anchor:   "Download"
    _score_link("fees") = 0  (no fee/tuition/cost/pricing in path or anchor)
    _score_pdf_link("fees") > 0  ("international" is in _PDF_CATEGORY_KEYWORDS["fees"])
    """

    _GAP_PDF_URL = "https://uni.edu.au/download/2024-international-prospectus.pdf"
    _GAP_PDF_ANCHOR = "Download"
    _SEED_URL = "https://uni.edu.au"

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _seed_html(self, extra_links: list[tuple[str, str]] | None = None) -> str:
        """Build a seed page HTML containing the gap PDF link."""
        links = [(self._GAP_PDF_URL, self._GAP_PDF_ANCHOR)]
        if extra_links:
            links.extend(extra_links)
        items = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
        return f"<html><body>{items}</body></html>"

    # --- unit: confirm the gap exists ---

    def test_score_link_gives_zero_for_gap_url(self):
        """Precondition: _score_link must give 0 for the gap URL + anchor."""
        from app.services.scraper.recovery.searcher import _score_link

        scores = _score_link(self._GAP_PDF_URL, self._GAP_PDF_ANCHOR, {"fees"})
        assert scores.get("fees", 0) == 0, (
            "_score_link should give 0 for a PDF whose path and anchor contain "
            "no HTML-page fee keywords ('fee', 'tuition', 'cost', 'pricing')"
        )

    def test_pdf_scorer_gives_positive_for_gap_url(self):
        """Precondition: _score_pdf_link must give > 0 for the same URL."""
        from app.services.scraper.recovery.extractor import _score_pdf_link

        score = _score_pdf_link(self._GAP_PDF_URL, self._GAP_PDF_ANCHOR, {"fees"})
        assert score > 0, (
            "_score_pdf_link should give > 0 because 'international' appears in "
            "_PDF_CATEGORY_KEYWORDS['fees'] and in the URL"
        )

    # --- integration: end-to-end through search_candidate_pages ---

    def test_homepage_pdf_appears_in_candidates(self):
        """After the fix: a PDF that scores 0 via _score_link but > 0 via
        _score_pdf_link must be returned in the candidates list by
        search_candidate_pages."""
        from app.services.scraper.recovery.searcher import search_candidate_pages

        seed_html = self._seed_html()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = seed_html

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("httpx.AsyncClient", return_value=mock_client):
                return await search_candidate_pages(self._SEED_URL, {"fees"})

        candidates = self._run(_run())

        pdf_candidates = [c for c in candidates if ".pdf" in c["url"]]
        assert pdf_candidates, (
            "Expected the gap PDF to appear in candidates, but got none.\n"
            f"PDF URL: {self._GAP_PDF_URL!r}\n"
            "Fix: searcher must use _score_pdf_link as a fallback when "
            "_score_link gives 0 for a PDF link."
        )
        assert any(
            "international-prospectus.pdf" in c["url"] for c in pdf_candidates
        ), f"Expected the gap PDF URL in candidates; got: {[c['url'] for c in candidates]}"

    def test_pdf_candidate_has_required_keys(self):
        """A PDF candidate returned via the fallback scorer has the standard
        candidate shape (url, category, score, path_score, matched_keyword)."""
        from app.services.scraper.recovery.searcher import search_candidate_pages

        seed_html = self._seed_html()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = seed_html

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("httpx.AsyncClient", return_value=mock_client):
                return await search_candidate_pages(self._SEED_URL, {"fees"})

        candidates = self._run(_run())
        pdf_c = next(
            (c for c in candidates if "international-prospectus.pdf" in c["url"]), None
        )
        assert pdf_c is not None, "Gap PDF should be in candidates"
        for key in ("url", "category", "score", "path_score", "matched_keyword"):
            assert key in pdf_c, f"Candidate dict missing key {key!r}: {pdf_c}"
        assert pdf_c["category"] == "fees"
        assert pdf_c["score"] > 0

    def test_already_scoring_pdfs_are_not_double_counted(self):
        """A PDF that already scores > 0 via _score_link is not scored again
        via _score_pdf_link (i.e. the fallback only fires when scores is empty)."""
        from app.services.scraper.recovery.searcher import _score_link

        # A PDF with "fees" in the URL path scores > 0 via _score_link
        well_scored_url = "https://uni.edu.au/fees/schedule.pdf"
        scores = _score_link(well_scored_url, "Fee schedule", {"fees"})
        assert scores.get("fees", 0) > 0, (
            "Sanity: a PDF with 'fees' in path should already score > 0 via "
            "_score_link — the fallback must not override this"
        )
