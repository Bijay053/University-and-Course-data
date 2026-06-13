"""Unit tests for the seen_pdf_urls dedup guard in extract_from_url.

A PDF URL that appears both as a direct candidate URL and as a linked PDF
discovered from another HTML page in the same recovery run must be fetched
exactly once.  The seen_pdf_urls set shared across all extract_from_url calls
for a university enforces this.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, call


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


# ---------------------------------------------------------------------------
# Test: linked PDF deduplication
# ---------------------------------------------------------------------------

class TestSeenPdfUrlsLinked:
    """PDF linked from one HTML page is skipped when the same PDF URL has
    already been registered in seen_pdf_urls from a previous call."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

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

            self._run(
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

            self._run(
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
            self._run(
                extract_from_url(
                    "https://uni.edu/page-one",
                    {"fees"},
                    seen_pdf_urls=seen,
                )
            )
            assert mock_pdf.call_count == 1

            # Second HTML page also links to the same PDF — must NOT re-fetch
            self._run(
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

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

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

            results = self._run(
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

            results = self._run(
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

            self._run(extract_from_url(pdf_url, {"fees"}))
            self._run(extract_from_url(pdf_url, {"fees"}))

            assert mock_pdf.call_count == 2


# ---------------------------------------------------------------------------
# Test: the full scenario — same PDF as direct candidate AND linked PDF
# ---------------------------------------------------------------------------

class TestSeenPdfUrlsFullScenario:
    """Integration scenario: PDF appears as a direct candidate URL *and* as a
    linked PDF on another HTML page.  The shared seen_pdf_urls set (as wired
    by run_recovery_pass) ensures it is only downloaded once."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

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
            # Call 1: direct PDF candidate
            mock_fetch.return_value = (None, "pdf_direct")
            extract_from_url_fn(pdf_url, {"fees"}, seen_pdf_urls=seen)
            assert mock_pdf.call_count == 1
            assert pdf_url in seen

            # Call 2: HTML page that links to the same PDF
            mock_fetch.return_value = (_FAKE_HTML, "html")
            extract_from_url_fn(html_url, {"fees"}, seen_pdf_urls=seen)
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
            # Call 1: HTML page links to the PDF
            mock_fetch.return_value = (_FAKE_HTML, "html")
            extract_from_url_fn(html_url, {"fees"}, seen_pdf_urls=seen)
            assert mock_pdf.call_count == 1
            assert pdf_url in seen

            # Call 2: same PDF URL is now processed as a direct candidate
            mock_fetch.return_value = (None, "pdf_direct")
            extract_from_url_fn(pdf_url, {"fees"}, seen_pdf_urls=seen)
            assert mock_pdf.call_count == 1  # still only fetched once


# Convenience alias — import inside each test method to avoid module-level
# import failures when the module has heavy optional deps.  But the full-
# scenario tests need the function imported in a shared helper to keep
# mock patches active across the two calls, so we define it at module level
# after the mock patches are established in the with-block.
def extract_from_url_fn(*args, **kwargs):
    from app.services.scraper.recovery.extractor import extract_from_url
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(extract_from_url(*args, **kwargs))
    finally:
        loop.close()
