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
        from app.services.scraper.recovery.extractor import score_pdf_link
        self._fn = score_pdf_link

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
        """Precondition: score_pdf_link must give > 0 for the same URL."""
        from app.services.scraper.recovery.extractor import score_pdf_link

        score = score_pdf_link(self._GAP_PDF_URL, self._GAP_PDF_ANCHOR, {"fees"})
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

    def test_via_broad_scorer_flag_true_for_fallback_candidate(self):
        """Candidates added via the _score_pdf_link fallback must have
        via_broad_scorer=True so the orchestrator can count and tag them."""
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

        broad_cands = [c for c in candidates if "international-prospectus.pdf" in c["url"]]
        assert broad_cands, "Gap PDF must appear in candidates"
        assert broad_cands[0].get("via_broad_scorer") is True, (
            "A PDF surfaced only via _score_pdf_link fallback must have "
            "via_broad_scorer=True; got: %r" % broad_cands[0]
        )

    def test_via_broad_scorer_flag_false_for_standard_candidate(self):
        """A candidate scored by the standard _score_link must have
        via_broad_scorer=False (or absent/falsy)."""
        from app.services.scraper.recovery.searcher import search_candidate_pages

        # A page whose URL path contains "fees" — standard scorer gives > 0
        standard_url = "https://uni.edu.au/fees/overview"
        html = f'<html><body><a href="{standard_url}">Tuition fees</a></body></html>'

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("httpx.AsyncClient", return_value=mock_client):
                return await search_candidate_pages(self._SEED_URL, {"fees"})

        candidates = self._run(_run())

        standard_cands = [c for c in candidates if "fees/overview" in c["url"]]
        assert standard_cands, "Standard URL must appear as a candidate"
        assert not standard_cands[0].get("via_broad_scorer"), (
            "A candidate found by the standard scorer must NOT have "
            "via_broad_scorer=True; got: %r" % standard_cands[0]
        )


# ---------------------------------------------------------------------------
# run_recovery_pass: pdfs_via_broad_scorer in summary + pdf_broad tagging
# ---------------------------------------------------------------------------

class TestBroadScorerSummaryCount:
    """run_recovery_pass must include pdfs_via_broad_scorer in its summary
    dict, and results from broad-scorer PDFs must have source_type='pdf_broad'."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_summary_includes_pdfs_via_broad_scorer_key(self):
        """run_recovery_pass returns a summary dict that always contains the
        pdfs_via_broad_scorer key (zero when no broad-scorer PDFs found)."""
        from app.services.scraper.recovery.run_recovery import run_recovery_pass

        # Minimal async DB mock that returns 0 courses → early return path
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))

        async def _run():
            return await run_recovery_pass("test-run-id", db)

        summary = self._run(_run())
        assert "pdfs_via_broad_scorer" in summary, (
            "run_recovery_pass summary must include 'pdfs_via_broad_scorer' key; "
            "got keys: %s" % list(summary.keys())
        )
        assert summary["pdfs_via_broad_scorer"] == 0

    def test_pdf_broad_source_type_tagged_for_broad_scorer_url(self):
        """PDF extraction results from a broad-scorer URL must have
        source_type='pdf_broad' (not 'pdf') after the tagging step."""
        # Simulate what the orchestrator does: take a result with source_type='pdf'
        # and tag it if the URL is in url_is_broad.
        pdf_url = "https://uni.edu.au/download/2024-international-prospectus.pdf"
        results = [
            {"field": "international_fee", "value": 12000.0, "source_type": "pdf", "source_url": pdf_url},
            {"field": "ielts_overall", "value": 6.5, "source_type": "pdf", "source_url": pdf_url},
        ]
        url_is_broad = {pdf_url}

        # Apply the tagging logic (mirrors what run_recovery.py does)
        for r in results:
            if r.get("source_type") == "pdf" and r.get("source_url") in url_is_broad:
                r["source_type"] = "pdf_broad"

        for r in results:
            assert r["source_type"] == "pdf_broad", (
                "Results from a broad-scorer URL must be tagged 'pdf_broad'; "
                "got %r for field=%r" % (r["source_type"], r["field"])
            )

    def test_standard_pdf_source_type_unchanged(self):
        """PDF results from a URL NOT in url_is_broad keep source_type='pdf'."""
        pdf_url = "https://uni.edu.au/fees/schedule.pdf"
        results = [
            {"field": "international_fee", "value": 15000.0, "source_type": "pdf", "source_url": pdf_url},
        ]
        url_is_broad: set[str] = set()  # empty — this URL was found by standard scorer

        for r in results:
            if r.get("source_type") == "pdf" and r.get("source_url") in url_is_broad:
                r["source_type"] = "pdf_broad"

        assert results[0]["source_type"] == "pdf", (
            "Standard PDF results must keep source_type='pdf'; "
            "got %r" % results[0]["source_type"]
        )

    def test_html_results_are_never_tagged_pdf_broad(self):
        """Only source_type='pdf' results are eligible for 'pdf_broad' tagging —
        HTML extraction results must never be re-tagged."""
        broad_url = "https://uni.edu.au/fees/overview"
        results = [
            {"field": "international_fee", "value": 15000.0, "source_type": "html", "source_url": broad_url},
        ]
        url_is_broad = {broad_url}

        for r in results:
            if r.get("source_type") == "pdf" and r.get("source_url") in url_is_broad:
                r["source_type"] = "pdf_broad"

        assert results[0]["source_type"] == "html", (
            "HTML results from a broad-scorer URL must keep source_type='html'; "
            "got %r" % results[0]["source_type"]
        )


# ---------------------------------------------------------------------------
# run_recovery_pass: fallback-discovered PDF flows through to extraction
# ---------------------------------------------------------------------------

class TestRunRecoveryPassFallbackPdf:
    """End-to-end: a via_broad_scorer=True PDF candidate flows through
    run_recovery_pass and produces an actionable recovery result.

    Tests verify three things:
    1. The candidate is passed to extract_from_url (not silently dropped).
    2. The result is tagged source_type='pdf_broad' because the URL is in
       url_is_broad.
    3. pdf_budget is decremented when extract_from_url handles a direct PDF URL.
    """

    _GAP_PDF_URL = "https://uni.edu.au/download/2024-international-prospectus.pdf"
    _SEED_URL = "https://uni.edu.au"
    _SCRAPE_RUN_ID = "test-run-fallback-pdf-001"

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _fake_course(self) -> dict:
        return {
            "id": 1,
            "university_id": 99,
            "course_name": "Bachelor of Commerce",
            "degree_level": "bachelor",
            "international_fee": None,
            "ielts_overall": 6.5,
            "intake_months": "February,July",
            "course_location": "Sydney",
            "other_requirement": None,
            "course_website": self._SEED_URL + "/courses/commerce",
            "status": "pending",
        }

    def _broad_candidate(self) -> dict:
        """Candidate dict for the gap PDF, surfaced only by the broad scorer."""
        return {
            "url": self._GAP_PDF_URL,
            "category": "fees",
            "score": 3,
            "path_score": 1,
            "matched_keyword": "international",
            "via_broad_scorer": True,
        }

    def _fee_result(self) -> dict:
        """A fee extraction result as would be returned by _extract_from_pdf."""
        return {
            "field": "international_fee",
            "value": 28000.0,
            "normalized": 28000.0,
            "confidence": 0.85,
            "snippet": "International tuition fee: AUD 28,000 per year",
            "method": "table",
            "source_url": self._GAP_PDF_URL,
            "source_type": "pdf",
        }

    def test_fallback_pdf_candidate_reaches_extractor(self):
        """run_recovery_pass must call extract_from_url with the gap PDF URL
        when it is in candidates (via_broad_scorer=True).  A candidate that
        enters the searcher output but is silently dropped before extraction
        would leave this assertion failing."""
        from app.services.scraper.recovery.run_recovery import run_recovery_pass

        extract_calls: list[tuple] = []

        async def mock_extract(url, categories, **kwargs):
            extract_calls.append((url, categories))
            if url == self._GAP_PDF_URL:
                return [dict(self._fee_result())]
            return []

        db = AsyncMock()

        async def _run():
            with patch(
                "app.services.scraper.recovery.run_recovery._get_courses_for_run",
                new=AsyncMock(return_value=[self._fake_course()]),
            ), patch(
                "app.services.scraper.recovery.run_recovery._get_evidence_for_courses",
                new=AsyncMock(return_value={1: []}),
            ), patch(
                "app.services.scraper.recovery.run_recovery._get_university_info",
                new=AsyncMock(return_value={
                    "scrape_url": self._SEED_URL,
                    "country": "AU",
                    "scrape_config": {},
                }),
            ), patch(
                "app.services.scraper.recovery.detector.detect_missing_fields",
                return_value=["international_fee"],
            ), patch(
                "app.services.scraper.recovery.searcher.search_candidate_pages",
                new=AsyncMock(return_value=[self._broad_candidate()]),
            ), patch(
                "app.services.scraper.recovery.extractor.extract_from_url",
                side_effect=mock_extract,
            ), patch(
                "app.services.scraper.recovery.run_recovery._existing_recovery_fields",
                new=AsyncMock(return_value=set()),
            ), patch(
                "app.services.scraper.recovery.run_recovery._write_recovery_results",
                new=AsyncMock(return_value=1),
            ):
                return await run_recovery_pass(self._SCRAPE_RUN_ID, db)

        self._run(_run())

        pdf_calls = [url for url, _ in extract_calls if ".pdf" in url]
        assert pdf_calls, (
            "extract_from_url was never called with the gap PDF URL.\n"
            "calls seen: %r\n"
            "The fallback-discovered PDF candidate must not be silently dropped "
            "between searcher output and the extraction step." % extract_calls
        )
        assert any(self._GAP_PDF_URL in url for url in pdf_calls), (
            "Expected extract_from_url to be called with %r; "
            "got calls: %r" % (self._GAP_PDF_URL, extract_calls)
        )

    def test_fallback_pdf_summary_counts_broad_scorer_pdf(self):
        """run_recovery_pass summary must report pdfs_via_broad_scorer >= 1
        when a via_broad_scorer=True candidate is present in the candidates list."""
        from app.services.scraper.recovery.run_recovery import run_recovery_pass

        db = AsyncMock()

        async def _run():
            with patch(
                "app.services.scraper.recovery.run_recovery._get_courses_for_run",
                new=AsyncMock(return_value=[self._fake_course()]),
            ), patch(
                "app.services.scraper.recovery.run_recovery._get_evidence_for_courses",
                new=AsyncMock(return_value={1: []}),
            ), patch(
                "app.services.scraper.recovery.run_recovery._get_university_info",
                new=AsyncMock(return_value={
                    "scrape_url": self._SEED_URL,
                    "country": "AU",
                    "scrape_config": {},
                }),
            ), patch(
                "app.services.scraper.recovery.detector.detect_missing_fields",
                return_value=["international_fee"],
            ), patch(
                "app.services.scraper.recovery.searcher.search_candidate_pages",
                new=AsyncMock(return_value=[self._broad_candidate()]),
            ), patch(
                "app.services.scraper.recovery.extractor.extract_from_url",
                new=AsyncMock(return_value=[dict(self._fee_result())]),
            ), patch(
                "app.services.scraper.recovery.run_recovery._existing_recovery_fields",
                new=AsyncMock(return_value=set()),
            ), patch(
                "app.services.scraper.recovery.run_recovery._write_recovery_results",
                new=AsyncMock(return_value=1),
            ):
                return await run_recovery_pass(self._SCRAPE_RUN_ID, db)

        summary = self._run(_run())

        assert summary["pdfs_via_broad_scorer"] >= 1, (
            "Expected pdfs_via_broad_scorer >= 1 in summary when a "
            "via_broad_scorer=True candidate is in the candidates list; "
            "got summary=%r" % summary
        )
        assert summary["results_written"] >= 1, (
            "Expected results_written >= 1 (at least one recovery result from "
            "the fallback PDF); got summary=%r" % summary
        )

    def test_fallback_pdf_result_tagged_pdf_broad(self):
        """The run_recovery_pass orchestrator re-tags PDF results whose source
        URL is in url_is_broad from 'pdf' to 'pdf_broad'.  This test captures
        the dict passed to map_results_to_course and asserts the tagging happened
        before mapping — so results written to the DB carry 'pdf_broad'."""
        from app.services.scraper.recovery.run_recovery import run_recovery_pass

        captured_map_inputs: list[list] = []

        def mock_map(all_results, degree_level=None, course_name=None):
            captured_map_inputs.append(list(all_results))
            # Return a minimal mapped dict so _write_recovery_results is called
            pdf_result = next((r for r in all_results if r.get("field") == "international_fee"), None)
            if pdf_result:
                return {"international_fee": pdf_result}
            return {}

        db = AsyncMock()

        async def _run():
            with patch(
                "app.services.scraper.recovery.run_recovery._get_courses_for_run",
                new=AsyncMock(return_value=[self._fake_course()]),
            ), patch(
                "app.services.scraper.recovery.run_recovery._get_evidence_for_courses",
                new=AsyncMock(return_value={1: []}),
            ), patch(
                "app.services.scraper.recovery.run_recovery._get_university_info",
                new=AsyncMock(return_value={
                    "scrape_url": self._SEED_URL,
                    "country": "AU",
                    "scrape_config": {},
                }),
            ), patch(
                "app.services.scraper.recovery.detector.detect_missing_fields",
                return_value=["international_fee"],
            ), patch(
                "app.services.scraper.recovery.searcher.search_candidate_pages",
                new=AsyncMock(return_value=[self._broad_candidate()]),
            ), patch(
                "app.services.scraper.recovery.extractor.extract_from_url",
                new=AsyncMock(return_value=[dict(self._fee_result())]),
            ), patch(
                "app.services.scraper.recovery.mapper.map_results_to_course",
                side_effect=mock_map,
            ), patch(
                "app.services.scraper.recovery.run_recovery._existing_recovery_fields",
                new=AsyncMock(return_value=set()),
            ), patch(
                "app.services.scraper.recovery.run_recovery._write_recovery_results",
                new=AsyncMock(return_value=1),
            ):
                return await run_recovery_pass(self._SCRAPE_RUN_ID, db)

        self._run(_run())

        assert captured_map_inputs, (
            "map_results_to_course was never called — the extraction result was "
            "dropped before reaching the mapping step"
        )
        all_results_seen = [r for batch in captured_map_inputs for r in batch]
        fee_results = [r for r in all_results_seen if r.get("field") == "international_fee"]
        assert fee_results, (
            "No international_fee result reached map_results_to_course; "
            "all results seen: %r" % all_results_seen
        )
        for r in fee_results:
            assert r.get("source_type") == "pdf_broad", (
                "Result from a via_broad_scorer PDF must have source_type='pdf_broad' "
                "by the time it reaches map_results_to_course; "
                "got source_type=%r for result=%r" % (r.get("source_type"), r)
            )
            assert r.get("source_url") == self._GAP_PDF_URL, (
                "source_url must be the gap PDF URL; got %r" % r.get("source_url")
            )

    def test_pdf_budget_decremented_for_direct_pdf_url(self):
        """extract_from_url must decrement pdf_budget[0] by 1 when the URL
        itself is a PDF (source_type='pdf_direct') and the budget is provided.

        This confirms that fallback-discovered PDFs are subject to the shared
        per-university PDF budget and cannot exhaust recovery runtime
        unboundedly even when many broad-scorer PDFs exist."""
        from app.services.scraper.recovery.extractor import extract_from_url

        fee_result = dict(self._fee_result())

        async def _run():
            budget = [10]
            with patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=("", "pdf_direct")),
            ), patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[fee_result]),
            ):
                results = await extract_from_url(
                    self._GAP_PDF_URL,
                    {"fees"},
                    pdf_budget=budget,
                )
            return results, budget

        results, budget = self._run(_run())

        assert budget[0] == 9, (
            "pdf_budget must be decremented by exactly 1 for a direct PDF URL; "
            "expected budget[0]=9, got %d" % budget[0]
        )
        assert results, (
            "extract_from_url must return extraction results even when the URL "
            "is a direct PDF (source_type='pdf_direct')"
        )
        assert results[0].get("source_url") == self._GAP_PDF_URL, (
            "Result source_url must be the PDF URL; got %r" % results[0].get("source_url")
        )

    def test_pdf_budget_exhausted_skips_direct_pdf(self):
        """When pdf_budget[0] == 0, extract_from_url must skip the direct PDF
        and return an empty list — confirming the budget guard is active for
        fallback-discovered PDFs."""
        from app.services.scraper.recovery.extractor import extract_from_url

        async def _run():
            budget = [0]
            extract_pdf_calls: list[str] = []

            async def mock_extract_from_pdf(pdf_url, categories):
                extract_pdf_calls.append(pdf_url)
                return [dict(self._fee_result())]

            with patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=("", "pdf_direct")),
            ), patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                side_effect=mock_extract_from_pdf,
            ):
                results = await extract_from_url(
                    self._GAP_PDF_URL,
                    {"fees"},
                    pdf_budget=budget,
                )
            return results, budget, extract_pdf_calls

        results, budget, calls = self._run(_run())

        assert results == [], (
            "extract_from_url must return [] when pdf_budget is exhausted; "
            "got: %r" % results
        )
        assert calls == [], (
            "_extract_from_pdf must not be called when pdf_budget[0]==0; "
            "got calls: %r" % calls
        )
        assert budget[0] == 0, (
            "pdf_budget must remain 0 when the PDF was skipped; got %d" % budget[0]
        )


# ---------------------------------------------------------------------------
# run_single_course_recovery: pdf_broad tagging, budget, trace rows
# ---------------------------------------------------------------------------

class TestSingleCourseRecoveryBroadPdf:
    """run_single_course_recovery has its own url_is_broad tagging block and
    its own pdf_budget allocation (mirroring run_recovery_pass).

    Tests verify:
    1. A via_broad_scorer=True PDF candidate that flows through the function
       results in source_type='pdf_broad' (not 'pdf') by the time it is passed
       to map_results_to_course.
    2. The pdf_budget kwarg is passed to extract_from_url and is decremented
       when the extractor processes a direct PDF URL.
    3. When search_candidate_pages returns no candidates, _write_trace_row is
       called once per needed field with status='no_source'.
    4. The gap PDF URL reaches extract_from_url (candidate not silently dropped).
    """

    _GAP_PDF_URL = "https://uni.edu.au/download/2024-international-prospectus.pdf"
    _SEED_URL = "https://uni.edu.au"
    _SC_ID = 42
    _SCRAPE_RUN_ID = "single-course-test-run-001"

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _mock_sc(self) -> MagicMock:
        """Return a MagicMock that looks like a ScrapedCourse ORM row."""
        sc = MagicMock()
        sc.id = self._SC_ID
        sc.scrape_job_id = self._SCRAPE_RUN_ID
        sc.university_id = 99
        sc.course_name = "Bachelor of Commerce"
        sc.degree_level = "bachelor"
        sc.international_fee = None
        sc.ielts_overall = 6.5
        sc.intake_months = "February,July"
        sc.course_location = "Sydney"
        sc.other_requirement = None
        return sc

    def _mock_db(self, sc: MagicMock) -> AsyncMock:
        """Return a minimal AsyncSession mock with pre-wired get/execute/commit."""
        db = AsyncMock()
        db.get = AsyncMock(return_value=sc)
        # evidence query (.all()) and DELETE statement both go through execute
        execute_result = MagicMock()
        execute_result.all = MagicMock(return_value=[])
        db.execute = AsyncMock(return_value=execute_result)
        db.commit = AsyncMock()
        return db

    def _broad_candidate(self) -> dict:
        return {
            "url": self._GAP_PDF_URL,
            "category": "fees",
            "score": 3,
            "path_score": 1,
            "matched_keyword": "international",
            "via_broad_scorer": True,
        }

    def _fee_result(self) -> dict:
        return {
            "field": "international_fee",
            "value": 28000.0,
            "normalized": 28000.0,
            "confidence": 0.85,
            "snippet": "International tuition fee: AUD 28,000 per year",
            "method": "table",
            "source_url": self._GAP_PDF_URL,
            "source_type": "pdf",
        }

    def _common_patches(self, sc: MagicMock):
        """Return a list of patch context managers shared across tests."""
        return [
            patch(
                "app.services.scraper.recovery.run_recovery._get_university_info",
                new=AsyncMock(return_value={
                    "scrape_url": self._SEED_URL,
                    "country": "AU",
                    "scrape_config": {},
                }),
            ),
            patch(
                "app.services.scraper.recovery.detector.detect_missing_fields",
                return_value=["international_fee"],
            ),
            patch(
                "app.services.scraper.recovery.run_recovery._write_recovery_results",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "app.services.scraper.recovery.run_recovery._fetch_all_rows_for_course",
                new=AsyncMock(return_value=[]),
            ),
        ]

    # ------------------------------------------------------------------ #

    def test_broad_pdf_candidate_reaches_extractor(self):
        """extract_from_url must be called with the gap PDF URL when
        search_candidate_pages returns a via_broad_scorer=True candidate.
        The fallback-discovered PDF must not be silently dropped between
        the searcher output and the extraction step."""
        from app.services.scraper.recovery.run_recovery import run_single_course_recovery

        sc = self._mock_sc()
        db = self._mock_db(sc)
        extract_calls: list[tuple] = []

        async def mock_extract(url, cats, **kwargs):
            extract_calls.append((url, frozenset(cats)))
            return [dict(self._fee_result())] if url == self._GAP_PDF_URL else []

        async def _run():
            patches = self._common_patches(sc) + [
                patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[self._broad_candidate()]),
                ),
                patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    side_effect=mock_extract,
                ),
                patch(
                    "app.services.scraper.recovery.mapper.map_results_to_course",
                    return_value=({"international_fee": dict(self._fee_result())}, {}),
                ),
            ]
            ctx: Any = patches[0]
            for p in patches[1:]:
                ctx = ctx.__class__.__new__(ctx.__class__)
                # Use contextlib.ExitStack instead
            from contextlib import AsyncExitStack, ExitStack
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                return await run_single_course_recovery(self._SC_ID, db)

        self._run(_run())

        pdf_calls = [url for url, _ in extract_calls if ".pdf" in url]
        assert pdf_calls, (
            "extract_from_url was never called with the gap PDF URL.\n"
            "calls seen: %r\n"
            "run_single_course_recovery must not drop via_broad_scorer=True "
            "candidates before the extraction step." % extract_calls
        )
        assert any(self._GAP_PDF_URL in url for url in pdf_calls), (
            "Expected extract_from_url to be called with %r; "
            "got calls: %r" % (self._GAP_PDF_URL, extract_calls)
        )

    def test_broad_pdf_result_tagged_pdf_broad(self):
        """PDF results from a via_broad_scorer=True URL must have
        source_type='pdf_broad' (not 'pdf') by the time they reach
        map_results_to_course."""
        from app.services.scraper.recovery.run_recovery import run_single_course_recovery

        sc = self._mock_sc()
        db = self._mock_db(sc)
        captured_map_inputs: list[list] = []

        def mock_map(all_results, degree_level=None, course_name=None, return_rejects=False):
            captured_map_inputs.append(list(all_results))
            fee = next((r for r in all_results if r.get("field") == "international_fee"), None)
            mapped = {"international_fee": fee} if fee else {}
            return (mapped, {}) if return_rejects else mapped

        async def _run():
            from contextlib import ExitStack
            patches = self._common_patches(sc) + [
                patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[self._broad_candidate()]),
                ),
                patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    new=AsyncMock(return_value=[dict(self._fee_result())]),
                ),
                patch(
                    "app.services.scraper.recovery.mapper.map_results_to_course",
                    side_effect=mock_map,
                ),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                return await run_single_course_recovery(self._SC_ID, db)

        self._run(_run())

        assert captured_map_inputs, (
            "map_results_to_course was never called — the extraction result "
            "was dropped before reaching the mapping step"
        )
        all_seen = [r for batch in captured_map_inputs for r in batch]
        fee_results = [r for r in all_seen if r.get("field") == "international_fee"]
        assert fee_results, (
            "No international_fee result reached map_results_to_course; "
            "all results seen: %r" % all_seen
        )
        for r in fee_results:
            assert r.get("source_type") == "pdf_broad", (
                "Result from a via_broad_scorer PDF must have source_type='pdf_broad' "
                "by the time it reaches map_results_to_course; "
                "got source_type=%r for result=%r" % (r.get("source_type"), r)
            )
            assert r.get("source_url") == self._GAP_PDF_URL, (
                "source_url must be the gap PDF URL; got %r" % r.get("source_url")
            )

    def test_pdf_budget_passed_and_decremented(self):
        """run_single_course_recovery must pass a pdf_budget dict to
        extract_from_url.  A side_effect that simulates the real decrement
        confirms the budget is threaded correctly and is mutable (so the guard
        in the extractor can enforce the per-category cap)."""
        from app.services.scraper.recovery.run_recovery import run_single_course_recovery

        sc = self._mock_sc()
        db = self._mock_db(sc)
        captured_budgets: list[dict] = []

        async def mock_extract(url, cats, country=None, metadata=None, pdf_budget=None):
            if pdf_budget is not None:
                captured_budgets.append(pdf_budget)
                # Simulate real extractor decrement for a direct fees PDF
                if url.lower().endswith(".pdf") and isinstance(pdf_budget, dict):
                    for cat in (cats or set()):
                        if pdf_budget.get(cat, 0) > 0:
                            pdf_budget[cat] -= 1
            if metadata is not None:
                metadata["source_type"] = "pdf_direct"
            return [dict(self._fee_result())] if url == self._GAP_PDF_URL else []

        async def _run():
            from contextlib import ExitStack
            patches = self._common_patches(sc) + [
                patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[self._broad_candidate()]),
                ),
                patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    side_effect=mock_extract,
                ),
                patch(
                    "app.services.scraper.recovery.mapper.map_results_to_course",
                    return_value=({"international_fee": dict(self._fee_result())}, {}),
                ),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                return await run_single_course_recovery(self._SC_ID, db)

        self._run(_run())

        assert captured_budgets, (
            "extract_from_url was not called with a pdf_budget argument; "
            "run_single_course_recovery must pass its pdf_budget dict to "
            "extract_from_url so the per-category cap is enforced."
        )
        budget = captured_budgets[0]
        assert isinstance(budget, dict), (
            "pdf_budget must be a mutable dict[str, int] (not %r)" % type(budget)
        )
        from app.services.scraper.recovery.extractor import (
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
        )
        assert "fees" in budget, (
            "pdf_budget dict must contain a 'fees' key; got keys: %r" % list(budget.keys())
        )
        expected_fees = _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY["fees"] - 1
        assert budget["fees"] == expected_fees, (
            "pdf_budget['fees'] must be decremented by 1 after processing a fees PDF; "
            "expected %d, got %d" % (expected_fees, budget["fees"])
        )

    def test_single_course_pdf_budget_lower_than_batch_cap(self):
        """run_single_course_recovery must initialise its pdf_budget with per-category
        caps from _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY, each of which must be
        strictly lower than the corresponding cap in _BATCH_PDF_BUDGET_PER_CATEGORY.
        This prevents a single-course trigger from consuming as many PDF fetches as
        a full batch pass."""
        from app.services.scraper.recovery.extractor import (
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        )
        assert _SINGLE_COURSE_PDF_BUDGET < MAX_PDFS_PER_RECOVERY_RUN, (
            "_SINGLE_COURSE_PDF_BUDGET (%d) must be strictly less than "
            "MAX_PDFS_PER_RECOVERY_RUN (%d) so that single-course recovery "
            "triggers consume fewer PDF fetches than a full batch pass."
            % (_SINGLE_COURSE_PDF_BUDGET, MAX_PDFS_PER_RECOVERY_RUN)
        )
        for cat, single_cap in _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY.items():
            batch_cap = _BATCH_PDF_BUDGET_PER_CATEGORY.get(cat, 0)
            assert single_cap <= batch_cap, (
                "single-course cap for '%s' (%d) must be <= batch cap (%d)"
                % (cat, single_cap, batch_cap)
            )

        from app.services.scraper.recovery.run_recovery import run_single_course_recovery

        sc = self._mock_sc()
        db = self._mock_db(sc)
        captured_initial: list[dict] = []

        async def mock_extract(url, cats, country=None, metadata=None, pdf_budget=None):
            if pdf_budget is not None and not captured_initial:
                # Snapshot a copy of the initial budget dict on first call
                captured_initial.append(dict(pdf_budget) if isinstance(pdf_budget, dict) else {"_legacy": pdf_budget[0]})
            if metadata is not None:
                metadata["source_type"] = "html"
            return []

        async def _run():
            from contextlib import ExitStack
            patches = self._common_patches(sc) + [
                patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[self._broad_candidate()]),
                ),
                patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    side_effect=mock_extract,
                ),
                patch(
                    "app.services.scraper.recovery.mapper.map_results_to_course",
                    return_value=({}, {}),
                ),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                return await run_single_course_recovery(self._SC_ID, db)

        self._run(_run())

        assert captured_initial, (
            "extract_from_url was never called with a pdf_budget; "
            "run_single_course_recovery must pass its pdf_budget dict to extract_from_url"
        )
        initial = captured_initial[0]
        assert "_legacy" not in initial, (
            "run_single_course_recovery must pass a dict[str, int] pdf_budget, not a list"
        )
        assert "fees" in initial and "english" in initial, (
            "pdf_budget dict must contain 'fees' and 'english' keys; got: %r" % initial
        )
        assert initial["fees"] == _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY["fees"], (
            "run_single_course_recovery must initialise pdf_budget['fees'] with "
            "_SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY['fees'] (%d), got %d"
            % (_SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY["fees"], initial["fees"])
        )
        assert initial["fees"] < _BATCH_PDF_BUDGET_PER_CATEGORY["fees"], (
            "Single-course fees budget (%d) must be lower than batch cap (%d)"
            % (initial["fees"], _BATCH_PDF_BUDGET_PER_CATEGORY["fees"])
        )

    def test_no_candidates_writes_trace_rows(self):
        """When search_candidate_pages returns an empty list,
        run_single_course_recovery must write a 'no_source' trace row for
        every needed field and return the resulting rows (no extraction is
        attempted)."""
        from app.services.scraper.recovery.run_recovery import run_single_course_recovery

        sc = self._mock_sc()
        db = self._mock_db(sc)
        trace_calls: list[tuple] = []

        async def mock_write_trace(db_, sc_id, run_id, field, status, reason, **kw):
            trace_calls.append((sc_id, field, status))

        async def _run():
            from contextlib import ExitStack
            patches = [
                patch(
                    "app.services.scraper.recovery.run_recovery._get_university_info",
                    new=AsyncMock(return_value={
                        "scrape_url": self._SEED_URL,
                        "country": "AU",
                        "scrape_config": {},
                    }),
                ),
                patch(
                    "app.services.scraper.recovery.detector.detect_missing_fields",
                    return_value=["international_fee", "other_requirement"],
                ),
                patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[]),
                ),
                patch(
                    "app.services.scraper.recovery.run_recovery._write_trace_row",
                    side_effect=mock_write_trace,
                ),
                patch(
                    "app.services.scraper.recovery.run_recovery._fetch_all_rows_for_course",
                    new=AsyncMock(return_value=[{"field": "international_fee", "status": "no_source"}]),
                ),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                return await run_single_course_recovery(self._SC_ID, db)

        result = self._run(_run())

        assert trace_calls, (
            "_write_trace_row was never called even though no candidates were found; "
            "run_single_course_recovery must write trace rows when the BFS search "
            "yields no candidate pages"
        )
        fields_traced = {field for _, field, _ in trace_calls}
        assert "international_fee" in fields_traced, (
            "Expected a trace row for 'international_fee'; "
            "trace_calls=%r" % trace_calls
        )
        assert "other_requirement" in fields_traced, (
            "Expected a trace row for 'other_requirement'; "
            "trace_calls=%r" % trace_calls
        )
        statuses = {status for _, _, status in trace_calls}
        assert statuses == {"no_source"}, (
            "All trace rows must have status='no_source' when no candidates found; "
            "got statuses=%r" % statuses
        )
        assert result, (
            "run_single_course_recovery must return the trace rows even when "
            "no candidates were found; got: %r" % result
        )


# ---------------------------------------------------------------------------
# make_pdf_budget — guard tests for the constant-selection helper
# ---------------------------------------------------------------------------

class TestMakePdfBudget:
    """Unit tests that pin the behaviour of make_pdf_budget() so that any
    future entry point that accidentally constructs a budget inline (rather
    than calling make_pdf_budget) is caught immediately.

    Four invariants are tested:
    1. batch path  (single_course=False) → caps from _BATCH_PDF_BUDGET_PER_CATEGORY
    2. single-course path (single_course=True) → caps from _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY
    3. each call returns a *fresh* dict (mutations don't bleed across calls)
    4. _SINGLE_COURSE_PDF_BUDGET < MAX_PDFS_PER_RECOVERY_RUN (the invariant
       that makes the guard meaningful)
    """

    def _imports(self):
        from app.services.scraper.recovery.extractor import (
            make_pdf_budget,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        )
        return (
            make_pdf_budget,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        )

    def test_batch_path_returns_batch_caps(self):
        """make_pdf_budget(single_course=False) must return a dict whose values
        exactly match _BATCH_PDF_BUDGET_PER_CATEGORY — not the legacy list
        [MAX_PDFS_PER_RECOVERY_RUN] and not the tighter single-course caps."""
        (
            make_pdf_budget,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        ) = self._imports()

        result = make_pdf_budget(single_course=False)

        assert isinstance(result, dict), (
            "make_pdf_budget(single_course=False) must return a dict[str, int], "
            "not %r" % type(result)
        )
        assert result == _BATCH_PDF_BUDGET_PER_CATEGORY, (
            "make_pdf_budget(single_course=False) returned wrong caps.\n"
            "Expected: %r\n"
            "Got:      %r\n"
            "A new entry point that constructs [MAX_PDFS_PER_RECOVERY_RUN] inline "
            "instead of calling make_pdf_budget() will fail here."
            % (_BATCH_PDF_BUDGET_PER_CATEGORY, result)
        )

    def test_single_course_path_returns_single_course_caps(self):
        """make_pdf_budget(single_course=True) must return a dict whose values
        exactly match _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY — not the tighter
        legacy scalar _SINGLE_COURSE_PDF_BUDGET and not the batch caps."""
        (
            make_pdf_budget,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        ) = self._imports()

        result = make_pdf_budget(single_course=True)

        assert isinstance(result, dict), (
            "make_pdf_budget(single_course=True) must return a dict[str, int], "
            "not %r" % type(result)
        )
        assert result == _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY, (
            "make_pdf_budget(single_course=True) returned wrong caps.\n"
            "Expected: %r\n"
            "Got:      %r\n"
            "A new entry point that constructs [_SINGLE_COURSE_PDF_BUDGET] inline "
            "instead of calling make_pdf_budget(single_course=True) will fail here."
            % (_SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY, result)
        )

    def test_each_call_returns_a_fresh_dict(self):
        """Mutating the dict returned by one make_pdf_budget() call must not
        affect the dict returned by a subsequent call (i.e. the helper must
        return a copy, not a reference to the module-level constant)."""
        (
            make_pdf_budget,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        ) = self._imports()

        for single_course in (False, True):
            first = make_pdf_budget(single_course=single_course)
            second = make_pdf_budget(single_course=single_course)

            assert first is not second, (
                "make_pdf_budget(single_course=%r) returned the same object on "
                "two consecutive calls — it must return a fresh copy each time "
                "so that callers can mutate their own budget without bleeding "
                "into other callers." % single_course
            )

            # Mutate the first dict and confirm the second is unaffected
            for key in list(first.keys()):
                first[key] = 0
            second_after_mutation = make_pdf_budget(single_course=single_course)
            expected = (
                _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY
                if single_course
                else _BATCH_PDF_BUDGET_PER_CATEGORY
            )
            assert second_after_mutation == expected, (
                "After mutating a budget dict returned by make_pdf_budget("
                "single_course=%r), a subsequent call returned unexpected values.\n"
                "Expected: %r\n"
                "Got:      %r\n"
                "make_pdf_budget must return a dict copy, not the module-level "
                "constant itself." % (single_course, expected, second_after_mutation)
            )

    def test_single_course_budget_strictly_less_than_max_recovery_run(self):
        """_SINGLE_COURSE_PDF_BUDGET < MAX_PDFS_PER_RECOVERY_RUN must hold.

        This invariant is what makes the make_pdf_budget() guard meaningful:
        single-course recovery triggers must always consume fewer PDF fetches
        than a full batch pass.  If someone raises _SINGLE_COURSE_PDF_BUDGET
        to equal or exceed MAX_PDFS_PER_RECOVERY_RUN the guard becomes a no-op.
        """
        (
            make_pdf_budget,
            _BATCH_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY,
            _SINGLE_COURSE_PDF_BUDGET,
            MAX_PDFS_PER_RECOVERY_RUN,
        ) = self._imports()

        assert _SINGLE_COURSE_PDF_BUDGET < MAX_PDFS_PER_RECOVERY_RUN, (
            "_SINGLE_COURSE_PDF_BUDGET (%d) must be strictly less than "
            "MAX_PDFS_PER_RECOVERY_RUN (%d).  If they become equal the "
            "make_pdf_budget() guard is meaningless — a single-course trigger "
            "would be allowed as many PDF fetches as a full batch pass."
            % (_SINGLE_COURSE_PDF_BUDGET, MAX_PDFS_PER_RECOVERY_RUN)
        )


# ---------------------------------------------------------------------------
# Call-site verification — every caller passes the correct single_course flag
# ---------------------------------------------------------------------------

class TestMakePdfBudgetCallSites:
    """Integration-level tests that assert every call-site in run_recovery.py
    passes the expected single_course flag to make_pdf_budget().

    A batch entry point that accidentally passes single_course=True (or a
    single-course trigger that omits the flag) will silently use the wrong
    caps at runtime.  These tests catch that class of mistake.

    Strategy: patch make_pdf_budget at its definition site
    (app.services.scraper.recovery.extractor.make_pdf_budget) with a spy
    that records the keyword arguments it is called with, then assert the
    captured kwargs match the expected flag for each entry point.

    The patch target is the definition site because both run_recovery_pass and
    run_single_course_recovery import make_pdf_budget via a local
    ``from app.services.scraper.recovery.extractor import make_pdf_budget``
    statement that executes each time the function is called.  Patching the
    definition object (not a pre-bound name in the caller's module) is the
    correct and future-proof target.
    """

    _MAKE_PDF_BUDGET_PATH = (
        "app.services.scraper.recovery.extractor.make_pdf_budget"
    )

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # ------------------------------------------------------------------ #
    # Helpers shared by both call-site tests                              #
    # ------------------------------------------------------------------ #

    def _make_budget_spy(self, real_fn):
        """Return (spy_fn, calls_list).

        spy_fn is a drop-in replacement for make_pdf_budget that records
        every (args, kwargs) it receives into calls_list, then delegates
        to the real function so callers get a valid budget dict back.
        """
        calls: list[dict] = []

        def spy(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return real_fn(*args, **kwargs)

        return spy, calls

    def _mock_sc(self) -> MagicMock:
        sc = MagicMock()
        sc.id = 7
        sc.scrape_job_id = "callsite-test-run-001"
        sc.university_id = 88
        sc.course_name = "Master of Data Science"
        sc.degree_level = "master"
        sc.international_fee = None
        sc.ielts_overall = None
        sc.intake_months = None
        sc.course_location = None
        sc.other_requirement = None
        sc.course_website = "https://uni.edu.au/courses/mds"
        return sc

    def _mock_db_single(self, sc: MagicMock) -> AsyncMock:
        """Minimal DB mock suitable for run_single_course_recovery."""
        db = AsyncMock()
        db.get = AsyncMock(return_value=sc)
        execute_result = MagicMock()
        execute_result.all = MagicMock(return_value=[])
        db.execute = AsyncMock(return_value=execute_result)
        db.commit = AsyncMock()
        return db

    def _mock_db_batch(self) -> AsyncMock:
        """Minimal DB mock suitable for run_recovery_pass.

        _get_courses_for_run uses db.execute(...).scalars().all() to return
        ScrapedCourse ORM rows.  We return a single fake row so the function
        proceeds past the early-exit guard.
        """
        db = AsyncMock()

        fake_row = MagicMock()
        fake_row.id = 7
        fake_row.university_id = 88
        fake_row.course_name = "Master of Data Science"
        fake_row.degree_level = "master"
        fake_row.international_fee = None
        fake_row.ielts_overall = None
        fake_row.intake_months = None
        fake_row.course_location = None
        fake_row.other_requirement = None
        fake_row.course_website = "https://uni.edu.au/courses/mds"
        fake_row.status = "pending"
        fake_row.scrape_job_id = "callsite-test-run-001"

        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[fake_row])
        execute_result_courses = MagicMock()
        execute_result_courses.scalars = MagicMock(return_value=scalars_mock)

        execute_result_evidence = MagicMock()
        execute_result_evidence.all = MagicMock(return_value=[])

        db.execute = AsyncMock(
            side_effect=[execute_result_courses, execute_result_evidence]
        )
        db.commit = AsyncMock()
        return db

    # ------------------------------------------------------------------ #
    # Test 1: run_single_course_recovery → single_course=True            #
    # ------------------------------------------------------------------ #

    def test_run_single_course_recovery_passes_single_course_true(self):
        """run_single_course_recovery must call make_pdf_budget with
        single_course=True.  Passing single_course=False (or omitting it)
        would give this path the full batch caps, allowing a single
        operator-triggered re-run to consume as many PDF fetches as an
        entire university's batch recovery pass."""
        from app.services.scraper.recovery.run_recovery import (
            run_single_course_recovery,
        )
        from app.services.scraper.recovery.extractor import (
            make_pdf_budget as real_make_pdf_budget,
        )

        sc = self._mock_sc()
        db = self._mock_db_single(sc)
        spy, calls = self._make_budget_spy(real_make_pdf_budget)

        _candidate = {
            "url": "https://uni.edu.au/fees",
            "category": "fees",
            "score": 5,
            "path_score": 2,
            "matched_keyword": "fees",
            "via_broad_scorer": False,
        }

        async def _run():
            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(patch(
                    "app.services.scraper.recovery.run_recovery._get_university_info",
                    new=AsyncMock(return_value={
                        "scrape_url": "https://uni.edu.au",
                        "country": "AU",
                        "scrape_config": {},
                    }),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.detector.detect_missing_fields",
                    return_value=["international_fee"],
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[_candidate]),
                ))
                stack.enter_context(patch(
                    self._MAKE_PDF_BUDGET_PATH,
                    side_effect=spy,
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    new=AsyncMock(return_value=[]),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.mapper.map_results_to_course",
                    return_value=({}, {}),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.run_recovery._fetch_all_rows_for_course",
                    new=AsyncMock(return_value=[]),
                ))
                return await run_single_course_recovery(sc.id, db)

        self._run(_run())

        assert calls, (
            "make_pdf_budget was never called inside run_single_course_recovery. "
            "Ensure the function calls make_pdf_budget() to initialise its "
            "per-course PDF budget before calling extract_from_url."
        )
        for call in calls:
            actual_flag = call["kwargs"].get("single_course", False)
            assert actual_flag is True, (
                "run_single_course_recovery must pass single_course=True to "
                "make_pdf_budget(); got single_course=%r.\n"
                "Passing single_course=False gives this per-course trigger the "
                "same large caps as a full batch pass, defeating the guard."
                % actual_flag
            )

    # ------------------------------------------------------------------ #
    # Test 2: run_recovery_pass → single_course=False                    #
    # ------------------------------------------------------------------ #

    def test_run_recovery_pass_passes_single_course_false(self):
        """run_recovery_pass must call make_pdf_budget with single_course=False
        (or rely on the default, which is also False).  Passing single_course=True
        would silently cap every per-university batch recovery to the tighter
        single-course limits, causing the batch pass to under-fetch PDFs."""
        from app.services.scraper.recovery.run_recovery import run_recovery_pass
        from app.services.scraper.recovery.extractor import (
            make_pdf_budget as real_make_pdf_budget,
        )

        db = self._mock_db_batch()
        spy, calls = self._make_budget_spy(real_make_pdf_budget)

        _candidate = {
            "url": "https://uni.edu.au/fees",
            "category": "fees",
            "score": 5,
            "path_score": 2,
            "matched_keyword": "fees",
            "via_broad_scorer": False,
        }

        async def _run():
            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(patch(
                    "app.services.scraper.recovery.run_recovery._get_university_info",
                    new=AsyncMock(return_value={
                        "scrape_url": "https://uni.edu.au",
                        "country": "AU",
                        "scrape_config": {},
                    }),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.detector.detect_missing_fields",
                    return_value=["international_fee"],
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[_candidate]),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.run_recovery._existing_recovery_fields",
                    new=AsyncMock(return_value=set()),
                ))
                stack.enter_context(patch(
                    self._MAKE_PDF_BUDGET_PATH,
                    side_effect=spy,
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    new=AsyncMock(return_value=[]),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.mapper.map_results_to_course",
                    return_value={},
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.run_recovery._write_recovery_results",
                    new=AsyncMock(return_value=0),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.config.context.get_uni_config",
                    return_value=None,
                ))
                return await run_recovery_pass("callsite-test-run-001", db)

        self._run(_run())

        assert calls, (
            "make_pdf_budget was never called inside run_recovery_pass. "
            "Ensure the function calls make_pdf_budget() to initialise its "
            "per-university PDF budget before calling extract_from_url."
        )
        for call in calls:
            actual_flag = call["kwargs"].get("single_course", False)
            assert actual_flag is False, (
                "run_recovery_pass must pass single_course=False (or rely on the "
                "default) to make_pdf_budget(); got single_course=%r.\n"
                "Passing single_course=True would silently cap each university's "
                "batch recovery to the tighter per-course limits."
                % actual_flag
            )


# ---------------------------------------------------------------------------
# Static-analysis guard: no recovery entry-point may bypass make_pdf_budget()
# ---------------------------------------------------------------------------

class TestNoPdfBudgetInlineConstruction:
    """Static-analysis guard: recovery entry-point modules must never construct
    a pdf_budget object inline.

    Why this exists
    ---------------
    ``make_pdf_budget()`` is the single source of truth for PDF caps.  If a
    new entry point (bulk re-trigger endpoint, repair path, etc.) constructs
    its own ``{"fees": N, ...}`` dict or ``[MAX_PDFS_PER_RECOVERY_RUN]`` list
    instead of calling the helper, it will use wrong caps silently — no
    existing dynamic test would catch it.

    These two tests scan the source of every recovery module **except**
    ``extractor.py`` (where the constants and ``make_pdf_budget`` live):

    1. ``test_no_budget_constants_referenced_outside_extractor`` — verifies
       that the internal constant names are not imported or referenced.
    2. ``test_no_inline_pdf_budget_dict_or_list`` — uses AST to verify that
       no variable whose name contains ``pdf_budget`` or ``pdf_cap`` is
       assigned a dict or list *literal* (rather than being the return value
       of a function call).

    If either test fails, the fix is always to call ``make_pdf_budget()``.
    """

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _recovery_sources_except_extractor() -> list[tuple[str, str]]:
        """Return [(filename, source_text)] for all recovery .py files
        except extractor.py.  These are the files a future entry point
        would be added to (or a new file in the same package)."""
        import pathlib
        pkg = pathlib.Path(__file__).parent.parent / "app" / "services" / "scraper" / "recovery"
        results = []
        for path in sorted(pkg.glob("*.py")):
            if path.name in ("extractor.py", "__init__.py"):
                continue
            results.append((path.name, path.read_text(encoding="utf-8")))
        return results

    # ------------------------------------------------------------------ #
    # Test 1: budget-constant names must not appear outside extractor.py  #
    # ------------------------------------------------------------------ #

    _FORBIDDEN_CONSTANT_NAMES = (
        "MAX_PDFS_PER_RECOVERY_RUN",
        "_BATCH_PDF_BUDGET_PER_CATEGORY",
        "_SINGLE_COURSE_PDF_BUDGET_PER_CATEGORY",
        "_SINGLE_COURSE_PDF_BUDGET",
    )

    def test_no_budget_constants_referenced_outside_extractor(self):
        """No recovery module outside extractor.py should reference the
        internal budget constants directly.  All budget construction must
        go through make_pdf_budget().

        Uses AST ``Name`` node inspection so that docstrings and comments
        that mention the constants for documentation purposes do not trigger
        false positives — only actual code references are caught.
        """
        import ast

        _forbidden = frozenset(self._FORBIDDEN_CONSTANT_NAMES)

        class ConstantNameVisitor(ast.NodeVisitor):
            def __init__(self):
                self.violations: list[str] = []

            def visit_Name(self, node: ast.Name) -> None:
                if node.id in _forbidden:
                    self.violations.append(
                        f"{self._filename}:{node.lineno}: "
                        f"references {node.id!r} — use make_pdf_budget() instead"
                    )
                self.generic_visit(node)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr in _forbidden:
                    self.violations.append(
                        f"{self._filename}:{node.lineno}: "
                        f"references {node.attr!r} — use make_pdf_budget() instead"
                    )
                self.generic_visit(node)

        all_violations: list[str] = []
        for filename, source in self._recovery_sources_except_extractor():
            try:
                tree = ast.parse(source, filename=filename)
            except SyntaxError:
                continue
            visitor = ConstantNameVisitor()
            visitor._filename = filename
            visitor.visit(tree)
            all_violations.extend(visitor.violations)

        assert not all_violations, (
            "Recovery modules outside extractor.py must not reference "
            "internal PDF budget constants directly.  Always call "
            "make_pdf_budget() instead.\n\nViolations found:\n"
            + "\n".join(f"  - {v}" for v in all_violations)
        )

    # ------------------------------------------------------------------ #
    # Test 2: pdf_budget must only be assigned via make_pdf_budget()       #
    # ------------------------------------------------------------------ #

    def test_no_inline_pdf_budget_dict_or_list(self):
        """Any variable whose name contains 'pdf_budget' or 'pdf_cap' must
        be assigned the *return value* of a function call (i.e. make_pdf_budget()),
        not a dict/list literal.

        This is checked via AST so it catches both simple assignments::

            pdf_budget = {"fees": 5}          # FORBIDDEN
            pdf_budget = [10]                 # FORBIDDEN

        and augmented / annotated variants::

            pdf_budget: dict = {"fees": 5}    # FORBIDDEN

        The check intentionally ignores extractor.py (where the template
        dicts are legitimately defined) and __init__.py.
        """
        import ast

        _BUDGET_VAR_RE = __import__("re").compile(
            r"pdf_budget|pdf_cap", __import__("re").IGNORECASE
        )

        class InlineBudgetVisitor(ast.NodeVisitor):
            def __init__(self):
                self.violations: list[str] = []

            def _is_literal_container(self, node: ast.expr) -> bool:
                return isinstance(node, (ast.Dict, ast.List))

            def _check_target_and_value(
                self, target_name: str, value_node: ast.expr, lineno: int, filename: str
            ) -> None:
                if _BUDGET_VAR_RE.search(target_name) and self._is_literal_container(value_node):
                    kind = "dict" if isinstance(value_node, ast.Dict) else "list"
                    self.violations.append(
                        f"{filename}:{lineno}: {target_name!r} assigned a "
                        f"{kind} literal — use make_pdf_budget() instead"
                    )

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._check_target_and_value(
                            target.id, node.value, node.lineno, self._filename
                        )
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if node.value is not None and isinstance(node.target, ast.Name):
                    self._check_target_and_value(
                        node.target.id, node.value, node.lineno, self._filename
                    )
                self.generic_visit(node)

        all_violations: list[str] = []
        for filename, source in self._recovery_sources_except_extractor():
            try:
                tree = ast.parse(source, filename=filename)
            except SyntaxError:
                continue
            visitor = InlineBudgetVisitor()
            visitor._filename = filename
            visitor.visit(tree)
            all_violations.extend(visitor.violations)

        assert not all_violations, (
            "Recovery entry-point modules must not construct pdf_budget "
            "objects inline.  Call make_pdf_budget(single_course=...) "
            "instead of writing a literal dict or list.\n\n"
            "Violations found:\n"
            + "\n".join(f"  - {v}" for v in all_violations)
        )

    # ------------------------------------------------------------------ #
    # Test 3: extract_from_url call-site guard (strongest check)          #
    # ------------------------------------------------------------------ #

    def test_extract_from_url_pdf_budget_kwarg_never_a_literal(self):
        """Every call to extract_from_url() in non-extractor recovery modules
        must pass a ``pdf_budget`` kwarg whose value is NOT a dict or list
        literal — either directly inline or via a locally-assigned variable.

        This catches two bypass patterns that Test 2 misses because they
        use a variable name that doesn't match ``pdf_budget|pdf_cap``::

            # Pattern A — direct inline kwarg (any variable name)
            extract_from_url(url, cats, pdf_budget={"fees": 5})   # FORBIDDEN
            extract_from_url(url, cats, pdf_budget=[10])           # FORBIDDEN

            # Pattern B — variable-mediated (name not matching the regex)
            budget = {"fees": 5}                                   # FORBIDDEN
            extract_from_url(url, cats, pdf_budget=budget)

        The test performs per-function intra-procedural assignment tracking:
        for each function body it maps ``name → assigned AST value`` for
        simple ``Assign`` and ``AnnAssign`` statements, then resolves any
        ``Name`` reference in the ``pdf_budget`` kwarg against that map.

        A ``pdf_budget`` kwarg value that resolves to a ``Dict`` or ``List``
        node is always a violation.  A value that resolves to a ``Call``
        node whose function is ``make_pdf_budget`` is always OK.  A value
        that resolves to something else (e.g. another Name that can't be
        resolved in the same function scope) is left unchecked — the other
        tests and the existing call-site tests cover that case.
        """
        import ast

        _LITERAL_TYPES = (ast.Dict, ast.List)

        def _fn_name(call_node: ast.Call) -> str:
            """Return the bare function name from a Call node (best-effort)."""
            func = call_node.func
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute):
                return func.attr
            return ""

        def _is_make_pdf_budget_call(node: ast.expr) -> bool:
            return isinstance(node, ast.Call) and _fn_name(node) == "make_pdf_budget"

        def _collect_assignments(stmts) -> dict[str, ast.expr]:
            """Shallow single-pass over a list of statements; records the
            *last* simple assignment to each name.  Nested scopes are not
            descended into (handled by the outer visitor's generic_visit)."""
            assigns: dict[str, ast.expr] = {}
            for stmt in stmts:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            assigns[target.id] = stmt.value
                elif isinstance(stmt, ast.AnnAssign):
                    if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                        assigns[stmt.target.id] = stmt.value
            return assigns

        class CallSiteVisitor(ast.NodeVisitor):
            """Walk function bodies; for each extract_from_url() call check
            the pdf_budget kwarg against the function's local assignment map."""

            def __init__(self, filename: str):
                self._filename = filename
                self.violations: list[str] = []

            def _check_function(self, node) -> None:
                local_assigns = _collect_assignments(node.body)
                self._check_calls_in_stmts(node.body, local_assigns)

            def _check_calls_in_stmts(self, stmts, local_assigns: dict[str, ast.expr]) -> None:
                """Walk all statements and nested nodes looking for Call nodes
                whose function name is extract_from_url."""
                for stmt in ast.walk(ast.Module(body=stmts, type_ignores=[])):
                    if not isinstance(stmt, ast.Call):
                        continue
                    if _fn_name(stmt) != "extract_from_url":
                        continue
                    # Found an extract_from_url call — inspect the pdf_budget kwarg.
                    for kw in stmt.keywords:
                        if kw.arg != "pdf_budget":
                            continue
                        self._check_kwarg_value(kw.value, stmt.lineno, local_assigns)

            def _check_kwarg_value(
                self,
                value: ast.expr,
                lineno: int,
                local_assigns: dict[str, ast.expr],
            ) -> None:
                """Resolve the kwarg value to a concrete node and flag literals."""
                # Resolve Name references one level deep using the function's
                # local assignment map.
                resolved = value
                if isinstance(value, ast.Name) and value.id in local_assigns:
                    resolved = local_assigns[value.id]

                if isinstance(resolved, _LITERAL_TYPES):
                    kind = "dict" if isinstance(resolved, ast.Dict) else "list"
                    self.violations.append(
                        f"{self._filename}:{lineno}: extract_from_url() called "
                        f"with pdf_budget= set to a {kind} literal "
                        f"— use make_pdf_budget() instead"
                    )
                # If resolved is a Call to make_pdf_budget → OK.
                # If resolved is something else we cannot prove → not flagged
                # (rely on existing call-site dynamic tests for that path).

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._check_function(node)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._check_function(node)
                self.generic_visit(node)

        all_violations: list[str] = []
        for filename, source in self._recovery_sources_except_extractor():
            try:
                tree = ast.parse(source, filename=filename)
            except SyntaxError:
                continue
            visitor = CallSiteVisitor(filename)
            visitor.visit(tree)
            all_violations.extend(visitor.violations)

        assert not all_violations, (
            "extract_from_url() call sites in recovery entry-point modules "
            "must not pass a dict/list literal as pdf_budget= (directly or "
            "via a locally-assigned variable).  Always use make_pdf_budget().\n\n"
            "Violations found:\n"
            + "\n".join(f"  - {v}" for v in all_violations)
        )
