"""Integration tests for the render_listing_pages link-harvesting pipeline.

These tests exercise the *actual* :func:`_apply_render_listing_pages` helper
that is called by ``run_scrape()`` — **not** a mirror helper, not a pure
unit of regex logic.  The Scrape.do HTTP layer is replaced by an injectable
``_fetch_fn`` so no real network call is made, but every other part of the
pipeline (HTML parsing, URL normalisation, same-host guard, allow/block
pattern matching, deduplication, emit callbacks) runs from the real
orchestrator code.

Why these tests exist
---------------------
The original bug: ``render_listing_pages`` applied ``allow_url_patterns``
against ``_path`` (path-only from ``urlparse``) instead of the full URL.
Because production YAML patterns often include the hostname — e.g.
``ulster\\.ac\\.uk/courses/`` — ``re.search(pattern, "/courses/foo")``
always returned ``None``, silently dropping *every* link the rendered pages
returned.  Result: 0 courses staged regardless of how many pages rendered.

These integration tests would have caught that regression at the time the
block was written and will catch it again if the filter is ever moved,
refactored, or the flag logic is inverted.
"""
from __future__ import annotations

import pytest

from app.services.scraper.orchestrator import _apply_render_listing_pages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_with_links(*hrefs: str) -> str:
    """Build minimal HTML whose only <a href> tags point at *hrefs*."""
    anchors = "\n".join(f'<a href="{h}">Course</a>' for h in hrefs)
    return f"<html><body>{anchors}</body></html>"


def _make_fetch(pages: dict[str, str]):
    """Return an async fetch stub that serves HTML from *pages* by URL.

    Any URL not in *pages* returns an empty string (simulates failure).
    Tracks which URLs were fetched and with what ``render`` flag.
    """
    calls: list[tuple[str, bool]] = []

    async def _fetch(url: str, *, render: bool = True) -> str:
        calls.append((url, render))
        return pages.get(url, "")

    _fetch.calls = calls  # type: ignore[attr-defined]
    return _fetch


async def _noop_emit(event: str, message: str, **kwargs) -> None:  # noqa: ANN003
    pass


# ---------------------------------------------------------------------------
# 1. Path-only allow_url_patterns — the current Ulster / Portsmouth style
# ---------------------------------------------------------------------------

class TestPathOnlyAllowPatterns:
    """Path-only patterns (e.g. /courses/[^/?]+) work with the full-URL filter."""

    @pytest.mark.asyncio
    async def test_matching_links_are_added(self):
        fetch = _make_fetch({
            "https://www.ulster.ac.uk/courses": _html_with_links(
                "/courses/accounting-bsc",
                "/courses/nursing-msc",
                "/study/applying",           # non-course — must be dropped
                "/doctoralcollege/phd-info",  # non-course — must be dropped
            ),
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.ulster.ac.uk",
            render_pages=["https://www.ulster.ac.uk/courses"],
            allow_patterns=[r"/courses/[^/?]+"],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        urls = {lk["url"] for lk in links}
        assert added == 2
        assert "https://www.ulster.ac.uk/courses/accounting-bsc" in urls
        assert "https://www.ulster.ac.uk/courses/nursing-msc" in urls
        assert "https://www.ulster.ac.uk/study/applying" not in urls
        assert "https://www.ulster.ac.uk/doctoralcollege/phd-info" not in urls

    @pytest.mark.asyncio
    async def test_bare_listing_root_is_dropped(self):
        """The listing root itself (/courses) must not be kept as a course link."""
        fetch = _make_fetch({
            "https://www.ulster.ac.uk/courses": _html_with_links("/courses"),
        })
        links: list[dict] = []
        await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.ulster.ac.uk",
            render_pages=["https://www.ulster.ac.uk/courses"],
            allow_patterns=[r"/courses/[^/?]+"],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert links == [], "bare /courses must not be kept"


# ---------------------------------------------------------------------------
# 2. Hostname-prefixed allow_url_patterns — e.g. port\.ac\.uk/study/courses/
# ---------------------------------------------------------------------------

class TestHostnamePrefixedAllowPatterns:
    """Hostname patterns work because the filter checks the full URL.

    This is the exact pattern Portsmouth uses.  Before the bug fix, the filter
    called re.search(pattern, path_only) — a hostname-including pattern never
    matches a bare path string, so every link was dropped.
    """

    @pytest.mark.asyncio
    async def test_hostname_pattern_keeps_matching_course(self):
        fetch = _make_fetch({
            "https://www.port.ac.uk/courses-listing": _html_with_links(
                "/study/courses/undergraduate/computing-bsc",
                "/study/courses/postgraduate/data-science-msc",
                "/study/study-skills/revision-help",  # must be dropped
            ),
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.port.ac.uk",
            render_pages=["https://www.port.ac.uk/courses-listing"],
            allow_patterns=[r"port\.ac\.uk/study/courses/[^?]+"],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        urls = {lk["url"] for lk in links}
        assert added == 2
        assert "https://www.port.ac.uk/study/courses/undergraduate/computing-bsc" in urls
        assert "https://www.port.ac.uk/study/courses/postgraduate/data-science-msc" in urls
        assert "https://www.port.ac.uk/study/study-skills/revision-help" not in urls

    @pytest.mark.asyncio
    async def test_old_path_only_logic_would_have_dropped_everything(self):
        """Regression reference: the pre-fix path-only logic rejects hostname patterns.

        This test *documents* what the broken code did so future readers can
        understand why the fix was necessary.  It does NOT call the orchestrator —
        it asserts the broken logic directly using ``re.search(pattern, path)``.
        """
        import re
        from urllib.parse import urlparse

        pattern = re.compile(r"port\.ac\.uk/study/courses/[^?]+")
        url = "https://www.port.ac.uk/study/courses/undergraduate/computing-bsc"
        path_only = urlparse(url).path  # "/study/courses/undergraduate/computing-bsc"

        # Path-only search: hostname pattern cannot match path-only string
        assert pattern.search(path_only) is None, (
            "The old (broken) path-only filter incorrectly drops this valid course URL"
        )
        # Full-URL search: works correctly
        assert pattern.search(url) is not None, (
            "The fixed full-URL filter keeps this valid course URL"
        )


# ---------------------------------------------------------------------------
# 3. block_url_patterns removes specific matching URLs
# ---------------------------------------------------------------------------

class TestBlockUrlPatterns:
    @pytest.mark.asyncio
    async def test_blocked_urls_are_excluded(self):
        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links(
                "/courses/good-course-bsc",
                "/courses/apply-now",      # blocked
                "/courses/open-day-info",  # blocked
            ),
        })
        links: list[dict] = []
        await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[r"/courses/[^/?]+"],
            block_patterns=[r"/apply", r"/open-day"],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        urls = {lk["url"] for lk in links}
        assert "https://www.example.edu/courses/good-course-bsc" in urls
        assert "https://www.example.edu/courses/apply-now" not in urls
        assert "https://www.example.edu/courses/open-day-info" not in urls

    @pytest.mark.asyncio
    async def test_block_applied_even_without_allow_pattern(self):
        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links(
                "/courses/good-course",
                "/courses/apply",
            ),
        })
        links: list[dict] = []
        await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[r"/apply"],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        urls = {lk["url"] for lk in links}
        assert "https://www.example.edu/courses/good-course" in urls
        assert "https://www.example.edu/courses/apply" not in urls


# ---------------------------------------------------------------------------
# 4. No allow_url_patterns — all same-host links pass (filter is a no-op)
# ---------------------------------------------------------------------------

class TestNoAllowPatterns:
    @pytest.mark.asyncio
    async def test_all_same_host_links_pass_when_no_allow_set(self):
        fetch = _make_fetch({
            "https://www.example.edu/listing": _html_with_links(
                "/courses/foo",
                "/about/bar",
                "/anything/else",
            ),
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/listing"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 3
        assert len(links) == 3


# ---------------------------------------------------------------------------
# 5. Deduplication — already-known links not added a second time
# ---------------------------------------------------------------------------

class TestDeduplication:
    @pytest.mark.asyncio
    async def test_existing_links_not_duplicated(self):
        pre_existing = [{"url": "https://www.example.edu/courses/existing-bsc", "name": "Existing"}]
        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links(
                "/courses/existing-bsc",  # already in links
                "/courses/new-msc",       # new
            ),
        })
        links = list(pre_existing)
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 1, "only the new link should be counted"
        assert len(links) == 2, "pre-existing link must not be duplicated"
        urls = {lk["url"] for lk in links}
        assert "https://www.example.edu/courses/existing-bsc" in urls
        assert "https://www.example.edu/courses/new-msc" in urls

    @pytest.mark.asyncio
    async def test_same_link_on_two_pages_added_only_once(self):
        html = _html_with_links("/courses/shared-course")
        fetch = _make_fetch({
            "https://www.example.edu/courses?page=1": html,
            "https://www.example.edu/courses?page=2": html,
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=[
                "https://www.example.edu/courses?page=1",
                "https://www.example.edu/courses?page=2",
            ],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 1, "link appearing on two pages must only be added once"
        assert len(links) == 1


# ---------------------------------------------------------------------------
# 6. Multiple listing pages — all are fetched
# ---------------------------------------------------------------------------

class TestMultipleListingPages:
    @pytest.mark.asyncio
    async def test_links_from_all_pages_collected(self):
        fetch = _make_fetch({
            "https://www.example.edu/courses?page=1": _html_with_links(
                "/courses/course-a", "/courses/course-b",
            ),
            "https://www.example.edu/courses?page=2": _html_with_links(
                "/courses/course-c", "/courses/course-d",
            ),
            "https://www.example.edu/courses?page=3": _html_with_links(
                "/courses/course-e",
            ),
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=[
                "https://www.example.edu/courses?page=1",
                "https://www.example.edu/courses?page=2",
                "https://www.example.edu/courses?page=3",
            ],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 5
        assert len(fetch.calls) == 3, "all 3 listing pages must be fetched"
        urls = {lk["url"] for lk in links}
        for slug in ("course-a", "course-b", "course-c", "course-d", "course-e"):
            assert f"https://www.example.edu/courses/{slug}" in urls


# ---------------------------------------------------------------------------
# 7. Same-host guard — external links silently ignored
# ---------------------------------------------------------------------------

class TestSameHostGuard:
    @pytest.mark.asyncio
    async def test_external_domain_links_ignored(self):
        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links(
                "https://www.otherdomain.com/courses/foo",  # external — must be dropped
                "/courses/valid-course",                    # same-host — must be kept
            ),
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 1
        assert links[0]["url"] == "https://www.example.edu/courses/valid-course"


# ---------------------------------------------------------------------------
# 8. render_static flag — controls render= kwarg passed to fetch_fn
# ---------------------------------------------------------------------------

class TestRenderStaticFlag:
    @pytest.mark.asyncio
    async def test_render_true_by_default(self):
        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links("/courses/bsc"),
        })
        await _apply_render_listing_pages(
            links=[],
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            render_static=False,
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert fetch.calls[0][1] is True, "render=True when render_static is False"

    @pytest.mark.asyncio
    async def test_render_false_when_render_static_set(self):
        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links("/courses/bsc"),
        })
        await _apply_render_listing_pages(
            links=[],
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            render_static=True,
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert fetch.calls[0][1] is False, "render=False when render_static is True"


# ---------------------------------------------------------------------------
# 9. Failed fetch (empty HTML) — returns 0 links, does not raise
# ---------------------------------------------------------------------------

async def _instant_sleep(_seconds: float) -> None:
    """No-op sleep injected into tests to skip the 12/24/36 s retry waits."""


class TestFailedFetch:
    @pytest.mark.asyncio
    async def test_empty_response_returns_zero_links(self):
        """fetch_fn returning '' → no links added, no exception raised.

        We inject _sleep_fn=_instant_sleep to avoid waiting 72 s per page
        for the three retry back-offs (12 + 24 + 36 s).
        """
        async def always_empty(url: str, *, render: bool = True) -> str:
            return ""

        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=[
                "https://www.example.edu/courses?page=1",
                "https://www.example.edu/courses?page=2",
            ],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=always_empty,
            _sleep_fn=_instant_sleep,
            emit=_noop_emit,
        )
        assert added == 0
        assert links == []

    @pytest.mark.asyncio
    async def test_one_page_fails_others_still_succeed(self):
        """A single page returning '' must not abort processing of subsequent pages."""
        fetch = _make_fetch({
            # page=1 missing → returns ""
            "https://www.example.edu/courses?page=2": _html_with_links("/courses/bsc"),
        })
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=[
                "https://www.example.edu/courses?page=1",
                "https://www.example.edu/courses?page=2",
            ],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            _sleep_fn=_instant_sleep,
            emit=_noop_emit,
        )
        assert added == 1, "page=2 links should still be collected even if page=1 failed"


# ---------------------------------------------------------------------------
# 10. Emit callbacks are called (status events bubble up to the caller)
# ---------------------------------------------------------------------------

class TestEmitCallbacks:
    @pytest.mark.asyncio
    async def test_status_emit_called_with_page_count(self):
        events: list[tuple] = []

        async def capture_emit(event: str, message: str, **kwargs) -> None:
            events.append((event, message))

        fetch = _make_fetch({
            "https://www.example.edu/courses": _html_with_links("/courses/bsc"),
        })
        await _apply_render_listing_pages(
            links=[],
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=capture_emit,
        )
        assert any("Scanning" in msg for _, msg in events), (
            "Initial status emit must mention scanning"
        )
        assert any("Rendered listing pages" in msg for _, msg in events), (
            "Completion emit must mention rendered listing pages"
        )

    @pytest.mark.asyncio
    async def test_no_completion_emit_when_zero_added(self):
        """When 0 links are added, the 'Rendered listing pages: +N' emit is suppressed."""
        events: list[tuple] = []

        async def capture_emit(event: str, message: str, **kwargs) -> None:
            events.append((event, message))

        async def empty_fetch(url: str, *, render: bool = True) -> str:
            return ""

        await _apply_render_listing_pages(
            links=[],
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=empty_fetch,
            _sleep_fn=_instant_sleep,
            emit=capture_emit,
        )
        assert not any("Rendered listing pages" in msg for _, msg in events), (
            "Completion emit must be suppressed when 0 links are added"
        )


# ---------------------------------------------------------------------------
# 11. Absolute hrefs in the HTML (not relative) are normalised correctly
# ---------------------------------------------------------------------------

class TestAbsoluteHrefs:
    @pytest.mark.asyncio
    async def test_absolute_same_host_href_is_kept(self):
        html = '<a href="https://www.example.edu/courses/bsc-computing">Course</a>'
        fetch = _make_fetch({"https://www.example.edu/courses": html})
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/courses"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 1
        assert links[0]["url"] == "https://www.example.edu/courses/bsc-computing"

    @pytest.mark.asyncio
    async def test_relative_non_slash_href_ignored(self):
        """Relative hrefs that don't start with '/' are skipped (ambiguous base)."""
        html = '<a href="courses/bsc-computing">Course</a>'
        fetch = _make_fetch({"https://www.example.edu/listing": html})
        links: list[dict] = []
        added = await _apply_render_listing_pages(
            links=links,
            scrape_url="https://www.example.edu",
            render_pages=["https://www.example.edu/listing"],
            allow_patterns=[],
            block_patterns=[],
            _fetch_fn=fetch,
            emit=_noop_emit,
        )
        assert added == 0, "non-slash relative hrefs must be skipped"
