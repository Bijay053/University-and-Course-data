"""Bounded raw-source transport for UEL's multi-award course pages."""
from __future__ import annotations

import asyncio

from app.services.scraper import http_fetcher


def has_uel_course_options(html: str | None) -> bool:
    """Reject bot shells and truncated responses before interpreting no routes."""
    if not html:
        return False
    return (
        "course-options-content-div" in html
        and "course-option-details__list-item" in html
        and "</html>" in html.lower()
    )


async def fetch_uel_source(url: str) -> str:
    """Prefer UEL's proven static proxy, with direct HTTP as an alternate.

    No internal selector reaches either transport. Each attempt is bounded;
    direct-first wastes up to 20 seconds per known-403 source on a catalogue.
    The caller owns retries and the normal job/cancellation deadline.
    """
    from app.services.scraper.extractors.uel_variants import uel_source_url

    source = uel_source_url(url)
    errors: list[str] = []
    for method, timeout in (("scrape_do_static", 45), ("http", 20)):
        try:
            if method == "http":
                html = await asyncio.wait_for(
                    http_fetcher.fetch_html(source, retries=0), timeout=timeout,
                )
            else:
                html = await asyncio.wait_for(
                    http_fetcher.fetch_html_scrape_do(source, render=False),
                    timeout=timeout,
                )
            if has_uel_course_options(html):
                return html
            errors.append(f"{method}: missing complete UEL course-options markup")
        except http_fetcher.ScrapedoAccountError:
            raise
        except Exception as exc:
            # Report transport/type, never provider response bodies or secrets.
            errors.append(f"{method}: {type(exc).__name__}")
    raise ValueError("UEL source fetch failed (" + "; ".join(errors) + ")")