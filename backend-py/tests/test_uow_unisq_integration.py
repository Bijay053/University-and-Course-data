"""Integration tests for UOW and UniSQ scraping accuracy.

These tests fetch REAL pages using Playwright and then run the full
extractor suite (fee, IELTS, intake, duration, location, study_mode)
against the rendered HTML.

They are marked ``integration`` and require:
  - Network access to uow.edu.au / unisq.edu.au
  - Playwright browser pool running (launched inside each test)

Run with:
    pytest tests/test_uow_unisq_integration.py -v --timeout=120

Skip in unit-test CI with: pytest -m "not integration"
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys

import pytest

log = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


async def _fetch_rendered(url: str, *, timeout_sec: int = 90) -> str | None:
    """Fetch fully-rendered HTML via Playwright (networkidle + 3s settle)."""
    try:
        from app.services.scraper.browser_pool import pool as bp
        rendered = await asyncio.wait_for(
            bp.fetch_html(
                url,
                wait_until="networkidle",
                settle_ms=3000,
                timeout=80_000,
            ),
            timeout=timeout_sec,
        )
        return rendered
    except Exception as exc:
        pytest.skip(f"Browser fetch failed (network/Playwright unavailable): {exc}")
        return None


async def _run_all_extractors(html: str, url: str) -> dict:
    """Run the extended extractor suite and return a flat payload dict."""
    from app.services.scraper.extractors import (
        duration,
        english_test,
        fee,
        intake,
        location,
        study_mode,
    )

    payload: dict = {}
    for extractor_mod in (fee, english_test, intake, duration, location, study_mode):
        try:
            results = await extractor_mod.extract(html, url)
        except Exception:
            continue
        for r in results:
            if not r.normalized:
                continue
            for k, v in r.normalized.items():
                if v in (None, "", 0, []):
                    continue
                payload.setdefault(k, v)
    return payload


# ---------------------------------------------------------------------------
# UOW — Bachelor of Arts (international, 2026)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uow_bachelor_of_arts_fee():
    url = "https://www.uow.edu.au/study/courses/bachelor-of-arts/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered, "Expected non-empty rendered HTML"
    payload = await _run_all_extractors(rendered, url)

    fee_val = payload.get("international_fee")
    assert fee_val is not None and fee_val > 0, (
        f"international_fee must be extracted from UOW BA page, got {fee_val!r}. "
        f"Extracted keys: {sorted(payload.keys())}"
    )
    assert 5_000 < fee_val < 200_000, f"Fee {fee_val} is outside plausible annual range"


@pytest.mark.asyncio
async def test_uow_bachelor_of_arts_ielts():
    url = "https://www.uow.edu.au/study/courses/bachelor-of-arts/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    ielts = payload.get("ielts_overall")
    assert ielts is not None and float(ielts) >= 5.0, (
        f"ielts_overall must be extracted from UOW BA page, got {ielts!r}. "
        f"Payload: {payload}"
    )


@pytest.mark.asyncio
async def test_uow_bachelor_of_arts_intake():
    url = "https://www.uow.edu.au/study/courses/bachelor-of-arts/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered
    from app.services.scraper.extractors.intake import extract as intake_extract
    results = await intake_extract(rendered, url)
    assert results, "Intake extractor must return at least one result for UOW BA"
    months = results[0].value
    assert isinstance(months, list) and months, f"intake_months must be non-empty list, got {months!r}"
    # UOW uses Autumn/Spring sessions → March and/or July
    _valid = {"February", "March", "July"}
    assert set(months) & _valid, (
        f"UOW BA intakes must contain at least one Autumn/Spring session month "
        f"(March or July), got {months}"
    )
    # Must NOT contain deadline months
    _bad = {"September", "November", "December", "October", "January", "May"}
    overlap = _bad & set(months)
    assert not overlap, f"UOW BA intake must not contain deadline months; got {overlap}"


# ---------------------------------------------------------------------------
# UOW — Bachelor of Business (international, 2026)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uow_bachelor_of_business_fee():
    url = "https://www.uow.edu.au/study/courses/bachelor-of-business/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    fee_val = payload.get("international_fee")
    assert fee_val is not None and fee_val > 0, (
        f"international_fee must be extracted from UOW BBA page, got {fee_val!r}"
    )
    assert 5_000 < fee_val < 200_000, f"Fee {fee_val} outside plausible range"


@pytest.mark.asyncio
async def test_uow_bachelor_of_business_ielts():
    url = "https://www.uow.edu.au/study/courses/bachelor-of-business/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    ielts = payload.get("ielts_overall")
    assert ielts is not None and float(ielts) >= 5.0, (
        f"ielts_overall must be extracted from UOW BBA page, got {ielts!r}"
    )


@pytest.mark.asyncio
async def test_uow_bachelor_of_business_duration():
    url = "https://www.uow.edu.au/study/courses/bachelor-of-business/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)
    dur = payload.get("duration_text") or payload.get("duration_years")
    assert dur, f"Duration must be extracted from UOW BBA page, got payload={payload}"


# ---------------------------------------------------------------------------
# UniSQ — Bachelor of Accounting (international)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unisq_bachelor_of_accounting_fee():
    url = "https://www.unisq.edu.au/study/degrees-and-courses/bachelor-of-accounting?studentType=international"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    fee_val = payload.get("international_fee")
    assert fee_val is not None and fee_val > 0, (
        f"international_fee must be extracted from UniSQ BA(acc) page, got {fee_val!r}. "
        f"Payload keys: {sorted(payload.keys())}"
    )
    assert 5_000 < fee_val < 200_000, f"Fee {fee_val} outside plausible range"


@pytest.mark.asyncio
async def test_unisq_bachelor_of_accounting_ielts():
    url = "https://www.unisq.edu.au/study/degrees-and-courses/bachelor-of-accounting?studentType=international"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    ielts = payload.get("ielts_overall")
    assert ielts is not None and float(ielts) >= 5.0, (
        f"ielts_overall must be extracted from UniSQ BA(acc) page, got {ielts!r}"
    )


# ---------------------------------------------------------------------------
# UniSQ — Master of Public Health (international)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unisq_master_of_public_health_fee():
    url = "https://www.unisq.edu.au/study/degrees-and-courses/master-of-public-health?studentType=international"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    fee_val = payload.get("international_fee")
    assert fee_val is not None and fee_val > 0, (
        f"international_fee must be extracted from UniSQ MPH page, got {fee_val!r}. "
        f"Payload keys: {sorted(payload.keys())}"
    )


@pytest.mark.asyncio
async def test_unisq_master_of_public_health_ielts():
    url = "https://www.unisq.edu.au/study/degrees-and-courses/master-of-public-health?studentType=international"
    rendered = await _fetch_rendered(url)
    assert rendered
    payload = await _run_all_extractors(rendered, url)

    ielts = payload.get("ielts_overall")
    assert ielts is not None and float(ielts) >= 5.0, (
        f"ielts_overall must be extracted from UniSQ MPH page, got {ielts!r}"
    )


# ---------------------------------------------------------------------------
# Smoke: rendered HTML is non-trivial (JS content loaded)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uow_rendered_html_is_substantial():
    """The rendered HTML should be substantially larger than the static
    shell — a tiny response indicates the browser didn't hydrate the SPA."""
    url = "https://www.uow.edu.au/study/courses/bachelor-of-arts/?students=international&year=2026"
    rendered = await _fetch_rendered(url)
    assert rendered
    assert len(rendered) > 50_000, (
        f"Rendered HTML looks too small ({len(rendered)} chars) — "
        "SPA may not have hydrated. Check networkidle config."
    )
    # Must contain fee or IELTS keywords
    text_lc = rendered.lower()
    assert any(kw in text_lc for kw in ("tuition", "fee", "ielts", "english")), (
        "Rendered page must contain at least one fee/IELTS keyword"
    )


@pytest.mark.asyncio
async def test_unisq_rendered_html_is_substantial():
    url = "https://www.unisq.edu.au/study/degrees-and-courses/bachelor-of-accounting?studentType=international"
    rendered = await _fetch_rendered(url)
    assert rendered
    assert len(rendered) > 30_000, (
        f"Rendered HTML looks too small ({len(rendered)} chars) for UniSQ"
    )
