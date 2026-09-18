"""DB regression coverage for re-extracting current-layout UEL routes."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import delete, select

from app.services.scraper.extractors.uel_variants import parse_uel_variants
from app.services.scraper.pipelines import single_course


SOURCE_URL = "https://www.uel.ac.uk/postgraduate/courses/msc-ai-data-science"
FIXTURES = Path(__file__).parent / "fixtures" / "uel_option_templates"
IELTS_FIELDS = (
    "ielts_overall",
    "ielts_writing",
    "ielts_speaking",
    "ielts_listening",
    "ielts_reading",
)


def _source_fixture() -> str:
    """Resolve the capture by provenance rather than coupling to its filename."""
    provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    matches = [
        name
        for name, record in provenance["fixtures"].items()
        if record["source_url"] == SOURCE_URL
    ]
    assert len(matches) == 1, f"expected one fixture for {SOURCE_URL}, got {matches}"
    return (FIXTURES / matches[0]).read_text(encoding="utf-8")


def _without_msc_requirements(html: str) -> str:
    """Remove only MSc-owned requirements, retaining the placement panel."""
    soup = BeautifulSoup(html, "html.parser")
    button = soup.find(
        "button",
        attrs={"aria-label": "Full entry requirements for MSc"},
    )
    assert button is not None
    modal_id = button.get("data-modal-id")
    modal = soup.find(id=modal_id)
    assert modal is not None
    button.decompose()
    modal.decompose()
    missing = str(soup)
    variants = {variant.key: variant for variant in parse_uel_variants(missing, SOURCE_URL)}
    assert "IELTS" not in variants["msc"].html
    assert "IELTS" in variants["msc-with-placement-year"].html
    return missing


@pytest.mark.asyncio
async def test_selected_route_reextract_is_scoped_and_clears_missing_owned_ielts(
    monkeypatch,
):
    """Fix one pending route in place without approving or changing its sibling."""
    from app.database import AsyncSessionLocal, engine
    from app.models import Course, ScrapeFeedback, ScrapedCourse, University
    from app.routers.scrape import ReExtractBody, re_extract_staged
    from app.services.scraper import http_fetcher, orchestrator, uel_transport
    from app.services.scraper.config import loader
    from app.services.scraper.stage_course import stage_course

    await engine.dispose()
    marker = uuid.uuid4().hex[:12]
    university_id = None
    html = _source_fixture()

    # The selected-route UEL transport is the only permitted fetch path.
    transport = AsyncMock(return_value=html)
    monkeypatch.setattr(uel_transport, "fetch_uel_source", transport)
    monkeypatch.setattr(
        http_fetcher,
        "fetch_html",
        AsyncMock(side_effect=AssertionError("unexpected external fetch")),
    )
    monkeypatch.setattr(
        http_fetcher,
        "fetch_html_scrape_do",
        AsyncMock(side_effect=AssertionError("unexpected paid fetch")),
    )
    monkeypatch.setattr(
        single_course,
        "fetch_html",
        AsyncMock(side_effect=AssertionError("unexpected generic fetch")),
    )
    monkeypatch.setattr(orchestrator, "_save_extraction_snapshot_safe", AsyncMock())
    # Keep this regression on course-owned HTML; no central-page prefetch.
    monkeypatch.setattr(loader, "get_config_for_host", lambda *args, **kwargs: None)

    try:
        async with AsyncSessionLocal() as db:
            university = University(
                name=f"UEL current-layout re-extract {marker}",
                country="United Kingdom",
                city="London",
                website="https://www.uel.ac.uk",
            )
            db.add(university)
            await db.commit()
            await db.refresh(university)
            university_id = university.id

            variants = parse_uel_variants(html, SOURCE_URL)
            assert [variant.key for variant in variants] == [
                "msc",
                "msc-with-placement-year",
            ]
            staged_rows = {}
            for variant in variants:
                extracted = await single_course.extract_course(
                    variant.url,
                    html=html,
                    country="United Kingdom",
                    use_ai_fallback=False,
                )
                # The reduced source capture intentionally retains the route
                # and requirements DOM, not UEL's separate fee content. Mark
                # that central source so the unrelated fee gate allows the
                # authentic extraction into staging.
                extracted["payload"]["has_central_fee_page"] = True
                staged = await stage_course(
                    db,
                    scrape_job_id=f"uel_current_layout_{marker}",
                    university_id=university_id,
                    course_name=extracted["payload"]["course_name"],
                    source_url=variant.url,
                    payload=extracted["payload"],
                    evidence=extracted["evidence"],
                )
                assert staged.saved, staged
                staged_rows[variant.key] = await db.get(
                    ScrapedCourse, staged.scraped_course_id
                )

            selected = staged_rows["msc"]
            sibling = staged_rows["msc-with-placement-year"]
            assert selected.status == sibling.status == "pending"
            assert selected.reviewed_at is sibling.reviewed_at is None

            # Reproduce the legacy mixed wrong/null bands.
            old_bands = (7.0, None, 5.0, None, 4.5)
            for field, value in zip(IELTS_FIELDS, old_bands):
                setattr(selected, field, value)
            db.add_all([
                ScrapeFeedback(
                    university_id=university_id,
                    scraped_course_id=selected.id,
                    course_name=selected.course_name,
                    field_key="ielts_overall",
                    issue_type="review_note",
                    reason="existing selected-route review",
                    status="active",
                ),
                ScrapeFeedback(
                    university_id=university_id,
                    scraped_course_id=sibling.id,
                    course_name=sibling.course_name,
                    field_key="ielts_overall",
                    issue_type="review_note",
                    reason="existing sibling review",
                    status="active",
                ),
            ])
            await db.commit()

            sibling_before = {
                column.name: getattr(sibling, column.name)
                for column in ScrapedCourse.__table__.columns
            }
            reviews_before = [
                (review.id, review.scraped_course_id, review.status, review.reason)
                for review in (
                    await db.execute(
                        select(ScrapeFeedback)
                        .where(ScrapeFeedback.university_id == university_id)
                        .order_by(ScrapeFeedback.id)
                    )
                ).scalars()
            ]

            result = await re_extract_staged(
                ReExtractBody(
                    ids=[selected.id],
                    universityId=university_id,
                    targetFields=["english_requirements"],
                ),
                db,
            )
            assert result["updated"] == 1, result
            await db.refresh(selected)
            await db.refresh(sibling)
            assert tuple(getattr(selected, field) for field in IELTS_FIELDS) == (
                6.0,
                6.0,
                6.0,
                5.5,
                5.5,
            )
            assert {
                column.name: getattr(sibling, column.name)
                for column in ScrapedCourse.__table__.columns
            } == sibling_before
            assert transport.await_count >= 1
            assert all(
                call.args[0] == selected.course_website
                for call in transport.await_args_list
            )

            # A later selected route with no owned requirements must clear old
            # values. The still-present sibling panel is not a fallback source.
            for field in IELTS_FIELDS:
                setattr(selected, field, 7.0)
            await db.commit()
            transport.reset_mock()
            transport.return_value = _without_msc_requirements(html)
            result = await re_extract_staged(
                ReExtractBody(
                    ids=[selected.id],
                    universityId=university_id,
                    targetFields=["english_requirements"],
                ),
                db,
            )
            assert result["updated"] == 1, result
            await db.refresh(selected)
            await db.refresh(sibling)
            assert all(getattr(selected, field) is None for field in IELTS_FIELDS)
            assert {
                column.name: getattr(sibling, column.name)
                for column in ScrapedCourse.__table__.columns
            } == sibling_before
            assert transport.await_count >= 1
            assert all(
                call.args[0] == selected.course_website
                for call in transport.await_args_list
            )

            reviews_after = [
                (review.id, review.scraped_course_id, review.status, review.reason)
                for review in (
                    await db.execute(
                        select(ScrapeFeedback)
                        .where(ScrapeFeedback.university_id == university_id)
                        .order_by(ScrapeFeedback.id)
                    )
                ).scalars()
            ]
            assert reviews_after == reviews_before
            assert (
                await db.execute(select(Course).where(Course.university_id == university_id))
            ).scalars().all() == []
            assert selected.status == sibling.status == "pending"
            assert selected.reviewed_at is sibling.reviewed_at is None
    finally:
        if university_id is not None:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(University).where(University.id == university_id))
                await db.commit()
        await engine.dispose()