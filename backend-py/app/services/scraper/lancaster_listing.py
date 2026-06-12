"""Lancaster University listing-page discovery — thin wrapper.

The actual logic now lives in the generic ``ssr_prop_discovery`` module.
This module exists for backward compatibility: universities that already have
``discovery.lancaster_listing: true`` in their YAML continue to work without
any YAML changes.

New universities should use ``discovery.ssr_prop_discovery:`` directly.
See ``ssr_prop_discovery.py`` and ``config/schema.py:SsrPropDiscoveryConfig``
for the full config reference.

Lancaster-specific defaults applied here:
  - Two listing pages (UG + PG on lancaster.ac.uk)
  - prop_attr: ``:courses-data``
  - year_field: ``entryYear``, year_filter_prefix: ``{short_year}/``
  - url_suffix: ``/{year}/``
  - browser_fallback_xpath: ``//nav[contains(@class, 'a-z')]//li/a/@href``
  - browser_wait_selector: ``nav.a-z``
"""
from __future__ import annotations

from typing import Any, Callable, Coroutine

from app.services.scraper.config.schema import (
    SsrPropDiscoveryConfig,
    SsrPropListingPageConfig,
)
from app.services.scraper.ssr_prop_discovery import fetch_ssr_prop_links

_LANCASTER_CONFIG = SsrPropDiscoveryConfig(
    listing_pages=[
        SsrPropListingPageConfig(
            url="https://www.lancaster.ac.uk/study/undergraduate/courses/",
            url_prefix="https://www.lancaster.ac.uk/study/undergraduate/courses/",
            label="Undergraduate",
        ),
        SsrPropListingPageConfig(
            url="https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/",
            url_prefix="https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/",
            label="Postgraduate",
        ),
    ],
    prop_attr=":courses-data",
    slug_field="slug",
    name_field="title",
    url_suffix="/{year}/",
    year_field="entryYear",
    year_filter_prefix="{short_year}/",
    browser_fallback_xpath="//nav[contains(@class, 'a-z')]//li/a/@href",
    browser_wait_selector="nav.a-z",
    course_url_pattern=(
        r"/study/(undergraduate/courses|postgraduate/postgraduate-courses)"
        r"/[^/]+/20\d{2}/?$"
    ),
)


async def fetch_lancaster_listing_links(
    year: int | None = None,
    *,
    emit: Callable[..., Coroutine[Any, Any, None]] | None = None,
) -> list[dict[str, str]]:
    """Fetch all Lancaster course URLs (backward-compat wrapper).

    Delegates to :func:`ssr_prop_discovery.fetch_ssr_prop_links` with
    Lancaster's hard-coded config.  Returns a list of
    ``{"name": ..., "url": ...}`` dicts.
    """
    return await fetch_ssr_prop_links(_LANCASTER_CONFIG, year=year, emit=emit)
