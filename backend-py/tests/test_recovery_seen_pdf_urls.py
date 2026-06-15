"""Unit tests for the seen_pdf_urls dedup guard in extract_from_url.

A PDF URL that appears both as a direct candidate URL and as a linked PDF
discovered from another HTML page in the same recovery run must be fetched
exactly once.  The seen_pdf_urls set shared across all extract_from_url calls
for a university enforces this.

Also verifies that pdf_budget[0] is decremented exactly once per unique PDF
URL — the budget counter must not be charged twice when a deduped PDF is
skipped via ``continue`` (linked path) or early ``return`` (direct path).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_HTML = (
    "<html><body>"
    "<a href='https://uni.edu/fees.pdf'>Fee schedule</a>"
    "</body></html>"
)

_DUMMY_RESULT = [
    {
        "field": "international_fee",
        "value": "30000",
        "normalized": 30000.0,
        "confidence": 0.9,
        "snippet": "",
        "method": "pdf",
        "source_url": "https://uni.edu/fees.pdf",
        "source_type": "pdf",
    }
]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Test: linked PDF deduplication
# ---------------------------------------------------------------------------

class TestSeenPdfUrlsLinked:
    """PDF linked from one HTML page is skipped when the same PDF URL has
    already been registered in seen_pdf_urls from a previous call."""

    def test_linked_pdf_skipped_when_already_seen(self):
        """A PDF linked from an HTML page is skipped if it was already added
        to seen_pdf_urls by a prior extract_from_url call in the same run."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = {pdf_url}  # pre-populate as if it was fetched directly

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    "https://uni.edu/admissions",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )

            mock_pdf.assert_not_called()

    def test_linked_pdf_fetched_when_not_yet_seen(self):
        """A PDF linked from an HTML page IS fetched when it is not yet in
        seen_pdf_urls, and the URL is then added to the set."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    "https://uni.edu/admissions",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )

            mock_pdf.assert_called_once_with(pdf_url, {"fees"})
            assert pdf_url in seen

    def test_pdf_url_added_to_seen_after_linked_fetch(self):
        """After a linked PDF is fetched, its URL is in seen_pdf_urls so a
        second call for a different HTML page skips it."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # First HTML page — fetches the PDF
            _run(
                extract_from_url(
                    "https://uni.edu/page-one",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1

            # Second HTML page also links to the same PDF — must NOT re-fetch
            _run(
                extract_from_url(
                    "https://uni.edu/page-two",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1  # still only 1


# ---------------------------------------------------------------------------
# Test: direct PDF (pdf_direct source_type) deduplication
# ---------------------------------------------------------------------------

class TestSeenPdfUrlsDirect:
    """A PDF URL that is itself a candidate (source_type=pdf_direct) is skipped
    when it is already in seen_pdf_urls."""

    def test_direct_pdf_skipped_when_already_seen(self):
        """When the candidate URL is a direct PDF and is already in
        seen_pdf_urls, _extract_from_pdf must not be called."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = {pdf_url}

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_direct")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            results = _run(
                extract_from_url(pdf_url, {"fees"}, seen_pdf_urls=seen)
            )

            mock_pdf.assert_not_called()
            assert results == []

    def test_direct_pdf_fetched_and_added_to_seen(self):
        """A direct PDF not yet in seen_pdf_urls is fetched and then added."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_direct")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            results = _run(
                extract_from_url(pdf_url, {"fees"}, seen_pdf_urls=seen)
            )

            mock_pdf.assert_called_once_with(pdf_url, {"fees"})
            assert pdf_url in seen
            assert results == _DUMMY_RESULT

    def test_direct_pdf_fetched_twice_without_seen_set(self):
        """When seen_pdf_urls is None (not provided), the same direct PDF
        URL is NOT deduplicated — existing behaviour is preserved."""
        pdf_url = "https://uni.edu/fees.pdf"

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_direct")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(extract_from_url(pdf_url, {"fees"}))
            _run(extract_from_url(pdf_url, {"fees"}))

            assert mock_pdf.call_count == 2


# ---------------------------------------------------------------------------
# Test: pdf_budget[0] is charged exactly once per unique PDF URL
# ---------------------------------------------------------------------------

class TestPdfBudgetNotDoubleCharged:
    """pdf_budget[0] must be decremented exactly once for each unique PDF URL.

    The seen_pdf_urls guard fires *before* the budget decrement on both the
    linked-PDF path (``continue``) and the direct-PDF path (``return``), so
    a deduped PDF must not consume a budget slot.
    """

    def test_linked_pdf_budget_decremented_once_across_two_html_pages(self):
        """Budget is decremented once when the same linked PDF URL is
        encountered from two different HTML pages in the same run."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = set()
        budget: list[int] = [5]

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    "https://uni.edu/page-a",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 4, "First encounter should consume one budget slot"

            _run(
                extract_from_url(
                    "https://uni.edu/page-b",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 4, "Deduped PDF must not consume another budget slot"

    def test_direct_pdf_budget_decremented_once_across_two_calls(self):
        """Budget is decremented exactly once when the same direct-PDF URL is
        passed to extract_from_url twice in the same run."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = set()
        budget: list[int] = [3]

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_direct")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    pdf_url, {"fees"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 2, "First direct-PDF call should decrement budget by 1"

            _run(
                extract_from_url(
                    pdf_url, {"fees"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 2, "Second call for same PDF must not touch budget"

    def test_direct_pdf_via_content_type_budget_decremented_once(self):
        """Same dedup behaviour applies for source_type='pdf_content_type'."""
        pdf_url = "https://uni.edu/fees.pdf"
        seen: set[str] = set()
        budget: list[int] = [3]

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_content_type")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    pdf_url, {"english"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 2

            _run(
                extract_from_url(
                    pdf_url, {"english"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 2, "pdf_content_type dedup must not double-charge budget"

    def test_cross_path_direct_then_linked_budget_charged_once(self):
        """Budget is decremented once when a URL is first seen as a direct PDF
        and then linked from a subsequent HTML page."""
        pdf_url = "https://uni.edu/international-fees.pdf"
        seen: set[str] = set()
        budget: list[int] = [5]

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
            ) as mock_fetch,
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: direct PDF candidate → budget 5→4
            mock_fetch.return_value = (None, "pdf_direct")
            _run(
                extract_from_url(
                    pdf_url, {"fees"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 4

            # Call 2: HTML page that links to the same PDF → no further budget change
            mock_fetch.return_value = (_FAKE_HTML, "html")
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 4, "Already-seen PDF must not charge budget again"

    def test_cross_path_linked_then_direct_budget_charged_once(self):
        """Budget is decremented once when a PDF is linked first from an HTML
        page and then also submitted as a direct candidate URL."""
        pdf_url = "https://uni.edu/international-fees.pdf"
        seen: set[str] = set()
        budget: list[int] = [5]

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
            ) as mock_fetch,
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: HTML page links to the PDF → budget 5→4
            mock_fetch.return_value = (_FAKE_HTML, "html")
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 4

            # Call 2: same PDF URL appears as a direct candidate → no budget change
            mock_fetch.return_value = (None, "pdf_direct")
            _run(
                extract_from_url(
                    pdf_url, {"fees"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 4, "Already-seen linked PDF must not charge budget when re-submitted as direct"

    def test_two_distinct_pdfs_charge_budget_independently(self):
        """Two distinct PDF URLs each charge the budget exactly once."""
        pdf_a = "https://uni.edu/fees-a.pdf"
        pdf_b = "https://uni.edu/fees-b.pdf"
        seen: set[str] = set()
        budget: list[int] = [5]

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
            ) as mock_links,
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            mock_links.return_value = [pdf_a, pdf_b]
            _run(
                extract_from_url(
                    "https://uni.edu/page-a",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 3, "Two distinct PDFs should each consume one budget slot"

            # Second call with the same two PDFs — both are now in seen, no further charge
            mock_links.return_value = [pdf_a, pdf_b]
            _run(
                extract_from_url(
                    "https://uni.edu/page-b",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget[0] == 3, "Already-seen PDFs must not charge budget again"


# ---------------------------------------------------------------------------
# Test: the full scenario — same PDF as direct candidate AND linked PDF
# ---------------------------------------------------------------------------

class TestSeenPdfUrlsFullScenario:
    """Integration scenario: PDF appears as a direct candidate URL *and* as a
    linked PDF on another HTML page.  The shared seen_pdf_urls set (as wired
    by run_recovery_pass) ensures it is only downloaded once."""

    def test_pdf_fetched_once_across_direct_and_linked(self):
        pdf_url = "https://uni.edu/international-fees.pdf"
        html_url = "https://uni.edu/fees-page"
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
            ) as mock_fetch,
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: direct PDF candidate
            mock_fetch.return_value = (None, "pdf_direct")
            _run(extract_from_url(pdf_url, {"fees"}, seen_pdf_urls=seen))
            assert mock_pdf.call_count == 1
            assert pdf_url in seen

            # Call 2: HTML page that links to the same PDF
            mock_fetch.return_value = (_FAKE_HTML, "html")
            _run(extract_from_url(html_url, {"fees"}, seen_pdf_urls=seen))
            # _extract_from_pdf must still be called only once total
            assert mock_pdf.call_count == 1

    def test_pdf_fetched_once_across_linked_then_direct(self):
        """PDF linked first from HTML, then also appears as direct candidate."""
        pdf_url = "https://uni.edu/international-fees.pdf"
        html_url = "https://uni.edu/fees-page"
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
            ) as mock_fetch,
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[pdf_url]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=_DUMMY_RESULT),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: HTML page links to the PDF
            mock_fetch.return_value = (_FAKE_HTML, "html")
            _run(extract_from_url(html_url, {"fees"}, seen_pdf_urls=seen))
            assert mock_pdf.call_count == 1
            assert pdf_url in seen

            # Call 2: same PDF URL is now processed as a direct candidate
            mock_fetch.return_value = (None, "pdf_direct")
            _run(extract_from_url(pdf_url, {"fees"}, seen_pdf_urls=seen))
            assert mock_pdf.call_count == 1  # still only fetched once


# ---------------------------------------------------------------------------
# Test: per-category budget isolation (dict[str, int] format)
# ---------------------------------------------------------------------------

_FAKE_HTML_WITH_TWO_PDFS = (
    "<html><body>"
    "<a href='https://uni.edu/fees.pdf'>Fee schedule</a>"
    "<a href='https://uni.edu/english.pdf'>English requirements</a>"
    "</body></html>"
)


class TestPerCategoryBudgetIsolation:
    """Exhausting the fees budget must NOT prevent English-requirements PDFs
    from being fetched, and vice-versa.

    These tests use the new dict[str, int] budget format returned by
    make_pdf_budget().
    """

    def test_fees_budget_exhausted_does_not_block_english_pdfs(self):
        """When fees budget is 0 but english budget is > 0, an english-category
        linked PDF must still be fetched."""
        fees_pdf = "https://uni.edu/fees.pdf"
        english_pdf = "https://uni.edu/english.pdf"
        budget: dict[str, int] = {"fees": 0, "english": 3}
        seen: set[str] = set()
        fetched_calls: list[tuple] = []

        async def mock_extract_from_pdf(pdf_url, cats):
            fetched_calls.append((pdf_url, frozenset(cats)))
            return []

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML_WITH_TWO_PDFS, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[fees_pdf, english_pdf]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                side_effect=mock_extract_from_pdf,
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    "https://uni.edu/admissions",
                    {"fees", "english"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )

        fetched_urls = [url for url, _ in fetched_calls]
        assert fees_pdf not in fetched_urls, (
            "fees PDF must be skipped when fees budget is 0; fetched: %r" % fetched_urls
        )
        assert english_pdf in fetched_urls, (
            "english PDF must still be fetched when fees budget is 0 but english budget > 0"
        )
        assert budget["fees"] == 0, "fees budget must remain 0"
        assert budget["english"] == 2, "english budget must be decremented by 1"

    def test_english_budget_exhausted_does_not_block_fees_pdfs(self):
        """Mirror of the above: english budget exhausted must not prevent fees PDFs."""
        fees_pdf = "https://uni.edu/fees.pdf"
        english_pdf = "https://uni.edu/english.pdf"
        budget: dict[str, int] = {"fees": 3, "english": 0}
        seen: set[str] = set()
        fetched_calls: list[tuple] = []

        async def mock_extract_from_pdf(pdf_url, cats):
            fetched_calls.append((pdf_url, frozenset(cats)))
            return []

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML_WITH_TWO_PDFS, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[fees_pdf, english_pdf]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                side_effect=mock_extract_from_pdf,
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    "https://uni.edu/admissions",
                    {"fees", "english"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )

        fetched_urls = [url for url, _ in fetched_calls]
        assert english_pdf not in fetched_urls, (
            "english PDF must be skipped when english budget is 0; fetched: %r" % fetched_urls
        )
        assert fees_pdf in fetched_urls, (
            "fees PDF must still be fetched when english budget is 0 but fees budget > 0"
        )
        assert budget["english"] == 0, "english budget must remain 0"
        assert budget["fees"] == 2, "fees budget must be decremented by 1"

    def test_both_budgets_exhausted_skips_all_pdfs(self):
        """When both fees and english budgets are 0, no PDFs are fetched."""
        fees_pdf = "https://uni.edu/fees.pdf"
        english_pdf = "https://uni.edu/english.pdf"
        budget: dict[str, int] = {"fees": 0, "english": 0}
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML_WITH_TWO_PDFS, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[fees_pdf, english_pdf]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(
                extract_from_url(
                    "https://uni.edu/admissions",
                    {"fees", "english"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )

        mock_pdf.assert_not_called()

    def test_make_pdf_budget_returns_dict_with_independent_categories(self):
        """make_pdf_budget() must return a dict[str, int] with independent per-category
        counters — fees and english must each have their own key."""
        from app.services.scraper.recovery.extractor import make_pdf_budget

        batch = make_pdf_budget(single_course=False)
        assert isinstance(batch, dict), (
            "make_pdf_budget() must return a dict; got %r" % type(batch)
        )
        assert "fees" in batch and "english" in batch, (
            "batch budget must have 'fees' and 'english' keys; got %r" % batch
        )
        assert batch["fees"] > 0 and batch["english"] > 0

        single = make_pdf_budget(single_course=True)
        assert isinstance(single, dict)
        assert "fees" in single and "english" in single
        assert single["fees"] > 0 and single["english"] > 0

        assert single["fees"] <= batch["fees"], (
            "single-course fees budget must be <= batch budget"
        )
        assert single["english"] <= batch["english"], (
            "single-course english budget must be <= batch budget"
        )

    def test_per_category_budgets_are_independent_across_calls(self):
        """Decrementing the fees budget must not affect the english budget
        and vice-versa when using the dict format."""
        from app.services.scraper.recovery.extractor import (
            _budget_decrement,
            _budget_remaining_categories,
        )

        budget: dict[str, int] = {"fees": 2, "english": 2}

        # Decrement fees twice
        _budget_decrement(budget, {"fees"})
        _budget_decrement(budget, {"fees"})

        assert budget["fees"] == 0, "fees should be 0 after 2 decrements"
        assert budget["english"] == 2, "english must be unaffected by fees decrements"

        # fees is exhausted; english still has budget
        remaining = _budget_remaining_categories(budget, {"fees", "english"})
        assert "english" in remaining, "english must still be in remaining when english budget > 0"
        assert "fees" not in remaining, "fees must not be in remaining when fees budget == 0"

    def test_skipped_pdf_not_marked_seen_so_later_english_call_can_fetch_it(self):
        """Critical regression: when a fees-exhausted call skips english.pdf, the URL
        must NOT be added to seen_pdf_urls — so a later extract_from_url call with
        categories={'english'} and remaining english budget can still fetch it.

        This was the original bug: seen_pdf_urls.add() fired before the budget check,
        so any PDF skipped by one category's exhausted budget was permanently blocked
        from other categories too.
        """
        english_pdf = "https://uni.edu/english-requirements.pdf"
        shared_seen: set[str] = set()
        shared_budget: dict[str, int] = {"fees": 0, "english": 3}
        fetched_urls: list[str] = []

        async def mock_extract_from_pdf(pdf_url, cats):
            fetched_urls.append(pdf_url)
            return []

        # Both calls link to english_pdf.  First call has categories={"fees"};
        # fees budget is 0, so the PDF is skipped — but must NOT be marked seen.
        # Second call has categories={"english"}; english budget is 3 — must fetch.
        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_FAKE_HTML_WITH_TWO_PDFS, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[english_pdf]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                side_effect=mock_extract_from_pdf,
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: fees page, fees budget exhausted — english.pdf skipped
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    pdf_budget=shared_budget,
                    seen_pdf_urls=shared_seen,
                )
            )
            assert english_pdf not in shared_seen, (
                "english.pdf must NOT be in seen_pdf_urls after being skipped by fees budget"
            )
            assert english_pdf not in fetched_urls, "english.pdf must not have been fetched yet"

            # Call 2: english page, english budget available — english.pdf must be fetched
            _run(
                extract_from_url(
                    "https://uni.edu/english-page",
                    {"english"},
                    pdf_budget=shared_budget,
                    seen_pdf_urls=shared_seen,
                )
            )
            assert english_pdf in fetched_urls, (
                "english.pdf must be fetched by the english-category call even though "
                "a previous fees-category call encountered and skipped it"
            )
            assert english_pdf in shared_seen, (
                "english.pdf must be in seen_pdf_urls after being successfully fetched"
            )
            assert shared_budget["english"] == 2, "english budget must be decremented by 1"

    def test_direct_pdf_fees_budget_exhausted_does_not_affect_english_call(self):
        """When a direct fees PDF exhausts its budget, a subsequent english-category
        extract_from_url call must still be able to fetch PDFs."""
        fees_pdf = "https://uni.edu/fees.pdf"
        english_pdf = "https://uni.edu/english.pdf"
        budget: dict[str, int] = {"fees": 1, "english": 1}
        seen: set[str] = set()

        with patch(
            "app.services.scraper.recovery.extractor._fetch_html",
            new=AsyncMock(return_value=(None, "pdf_direct")),
        ), patch(
            "app.services.scraper.recovery.extractor._extract_from_pdf",
            new=AsyncMock(return_value=[]),
        ) as mock_pdf:
            from app.services.scraper.recovery.extractor import extract_from_url

            # First direct PDF: fees — consumes fees budget (1→0)
            _run(
                extract_from_url(
                    fees_pdf, {"fees"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget["fees"] == 0
            assert budget["english"] == 1, "english budget must be unaffected"
            assert mock_pdf.call_count == 1

            # Second direct PDF: english — english budget still 1, should be fetched
            _run(
                extract_from_url(
                    english_pdf, {"english"}, pdf_budget=budget, seen_pdf_urls=seen,
                )
            )
            assert budget["english"] == 0
            assert mock_pdf.call_count == 2, (
                "english PDF must be fetched even though fees budget is exhausted"
            )


# ---------------------------------------------------------------------------
# Test: mixed-category PDF URL — the primary scenario from task-175
#
# A PDF like "international-fees-and-requirements.pdf" contains keywords for
# BOTH the "fees" category ("fee", "fees") AND the "requirements" category
# ("requirement").  When two separate extract_from_url calls are made — the
# first with categories={"fees"} and the second with categories={"requirements"}
# — the shared seen_pdf_urls set must ensure the PDF is fetched exactly once.
# ---------------------------------------------------------------------------

_MIXED_PDF_URL = "https://uni.edu/international-fees-and-requirements.pdf"

_HTML_LINKING_MIXED_PDF = (
    "<html><body>"
    f"<a href='{_MIXED_PDF_URL}'>Fees and Requirements</a>"
    "</body></html>"
)


class TestSeenPdfUrlsMixedCategory:
    """seen_pdf_urls deduplicates a PDF that scores for two separate categories
    when separate extract_from_url calls are made — one per category.

    The motivating URL is "international-fees-and-requirements.pdf" which
    contains both fee keywords and requirements keywords.  Without the
    seen_pdf_urls guard, a recovery orchestrator that issues one call per
    category would download the same PDF twice.
    """

    def test_mixed_pdf_url_scores_for_both_fees_and_requirements(self):
        """Precondition: confirm the mixed URL does score > 0 for both
        categories so the dedup scenario is realistic."""
        from app.services.scraper.recovery.extractor import score_pdf_link

        fees_score = score_pdf_link(_MIXED_PDF_URL, "Fees and Requirements", {"fees"})
        reqs_score = score_pdf_link(_MIXED_PDF_URL, "Fees and Requirements", {"requirements"})
        assert fees_score > 0, (
            f"Expected fees score > 0 for {_MIXED_PDF_URL!r}; got {fees_score}"
        )
        assert reqs_score > 0, (
            f"Expected requirements score > 0 for {_MIXED_PDF_URL!r}; got {reqs_score}"
        )

    def test_linked_mixed_pdf_fetched_once_across_two_category_calls(self):
        """A linked PDF that scores for both fees and requirements is fetched
        exactly once when two separate calls are made — first with {"fees"},
        then with {"requirements"} — sharing seen_pdf_urls."""
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_HTML_LINKING_MIXED_PDF, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[_MIXED_PDF_URL]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: fees category — PDF must be fetched and added to seen
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1, (
                "First call (fees) must fetch the mixed PDF exactly once"
            )
            assert _MIXED_PDF_URL in seen, (
                "Mixed PDF URL must be in seen_pdf_urls after first call"
            )

            # Call 2: requirements category — PDF must NOT be fetched again
            _run(
                extract_from_url(
                    "https://uni.edu/requirements-page",
                    {"requirements"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1, (
                "Second call (requirements) must skip the PDF because it is "
                "already in seen_pdf_urls — even though the URL scores for "
                "requirements too"
            )

    def test_direct_mixed_pdf_fetched_once_across_two_category_calls(self):
        """The same dedup guarantee holds when the mixed PDF URL appears as a
        direct candidate (pdf_direct source_type) in both calls."""
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_direct")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: fees — fetches and marks seen
            _run(
                extract_from_url(
                    _MIXED_PDF_URL, {"fees"}, seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1
            assert _MIXED_PDF_URL in seen

            # Call 2: requirements — must be skipped
            _run(
                extract_from_url(
                    _MIXED_PDF_URL, {"requirements"}, seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1, (
                "Direct mixed PDF submitted a second time with a different "
                "category must be skipped by the seen_pdf_urls guard"
            )

    def test_mixed_category_order_reversed_still_fetches_once(self):
        """Dedup works regardless of which category is encountered first."""
        seen: set[str] = set()

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_HTML_LINKING_MIXED_PDF, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[_MIXED_PDF_URL]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: requirements first this time
            _run(
                extract_from_url(
                    "https://uni.edu/requirements-page",
                    {"requirements"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1
            assert _MIXED_PDF_URL in seen

            # Call 2: fees — must be skipped
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1, (
                "PDF already in seen_pdf_urls must be skipped even when the "
                "second call uses a different category (fees vs requirements)"
            )

    def test_mixed_pdf_budget_charged_once_across_two_category_calls(self):
        """The per-category budget is decremented exactly once for the mixed
        PDF even when two separate calls are made with different categories."""
        seen: set[str] = set()
        budget: dict[str, int] = {"fees": 5, "requirements": 5}

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_HTML_LINKING_MIXED_PDF, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[_MIXED_PDF_URL]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ),
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            # Call 1: fees page — PDF fetched, fees budget decremented
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget["fees"] == 4, "fees budget must be decremented by 1 on first fetch"
            assert budget["requirements"] == 5, "requirements budget must be untouched"

            # Call 2: requirements page — PDF in seen, no budget consumed
            _run(
                extract_from_url(
                    "https://uni.edu/requirements-page",
                    {"requirements"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert budget["fees"] == 4, "fees budget must be unchanged after deduped call"
            assert budget["requirements"] == 5, (
                "requirements budget must not be decremented when PDF is skipped "
                "because it was already seen"
            )

    def test_make_pdf_budget_dict_only_first_category_decremented(self):
        """Using the dict returned by make_pdf_budget():
        - After the first call (categories={"fees"}), only budget["fees"] decrements.
        - After the deduped second call (categories={"requirements"}), neither
          budget["fees"] nor budget["requirements"] changes.
        This closes the gap where existing tests used hand-crafted dicts instead of
        the helper so a signature change to make_pdf_budget() would go undetected.
        """
        from app.services.scraper.recovery.extractor import (
            extract_from_url,
            make_pdf_budget,
        )

        seen: set[str] = set()
        budget = make_pdf_budget(single_course=False)

        # Record initial values so the test does not depend on magic numbers.
        initial_fees = budget["fees"]
        initial_requirements = budget["requirements"]
        assert initial_fees > 0, "make_pdf_budget must provide a non-zero fees budget"
        assert initial_requirements > 0, (
            "make_pdf_budget must provide a non-zero requirements budget"
        )

        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(_HTML_LINKING_MIXED_PDF, "html")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._run_extractor",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._find_linked_pdfs",
                new=AsyncMock(return_value=[_MIXED_PDF_URL]),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ) as mock_pdf,
        ):
            # Call 1: fees page — PDF fetched, only fees counter decremented.
            _run(
                extract_from_url(
                    "https://uni.edu/fees-page",
                    {"fees"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1, (
                "First call (fees) must fetch the mixed PDF"
            )
            assert budget["fees"] == initial_fees - 1, (
                "budget['fees'] must be decremented by exactly 1 on the first fetch"
            )
            assert budget["requirements"] == initial_requirements, (
                "budget['requirements'] must be unaffected by the fees-category call"
            )
            assert _MIXED_PDF_URL in seen, (
                "Mixed PDF URL must be added to seen_pdf_urls after the first fetch"
            )

            # Call 2: requirements page — PDF is already in seen, neither counter changes.
            _run(
                extract_from_url(
                    "https://uni.edu/requirements-page",
                    {"requirements"},
                    pdf_budget=budget,
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1, (
                "Second call (requirements) must skip the PDF because it is already "
                "in seen_pdf_urls — _extract_from_pdf must not be called again"
            )
            assert budget["fees"] == initial_fees - 1, (
                "budget['fees'] must not change after the deduped requirements call"
            )
            assert budget["requirements"] == initial_requirements, (
                "budget['requirements'] must not be decremented when the PDF is "
                "skipped via the seen_pdf_urls guard"
            )

    def test_without_seen_set_mixed_pdf_fetched_twice(self):
        """When seen_pdf_urls is None the same PDF URL is NOT deduplicated
        across separate calls — each call fetches it independently.
        This documents the existing (pre-seen_pdf_urls) behaviour."""
        with (
            patch(
                "app.services.scraper.recovery.extractor._fetch_html",
                new=AsyncMock(return_value=(None, "pdf_direct")),
            ),
            patch(
                "app.services.scraper.recovery.extractor._extract_from_pdf",
                new=AsyncMock(return_value=[]),
            ) as mock_pdf,
        ):
            from app.services.scraper.recovery.extractor import extract_from_url

            _run(extract_from_url(_MIXED_PDF_URL, {"fees"}))
            _run(extract_from_url(_MIXED_PDF_URL, {"requirements"}))

            assert mock_pdf.call_count == 2, (
                "Without seen_pdf_urls the same PDF is fetched once per call — "
                "callers must pass a shared seen set to get deduplication"
            )
