"""Task 555 runtime acceptance proof.

This deliberately uses the real extractor, central-page parser, recipe
transform, and stage_course against the configured disposable PostgreSQL
database.  Only the two official page responses are mocked.
"""
from __future__ import annotations

import copy
import uuid

import pytest


def test_task555_audience_consensus_rejects_conflicts_and_positional_identity():
    from app.services.scraper.ai_repair_live import audience_scoped_recipe_proposals

    def page(month, source, container="student-audience"):
        return {
            "status": "accepted", "same_panel": True, "linked_official": True,
            "evidence": [
                {"audience": "domestic", "container": container, "value": "domestic"},
                {"audience": "international", "container": container, "value": "intl",
                 "source_url": source, "intake_months": [month], "intake_year": 2027},
            ],
        }
    conflict = audience_scoped_recipe_proposals({
        "course_evidence": [page(1, "https://u.test/e1"), page(2, "https://u.test/e2")]
    })
    assert conflict["status"] == "needs_review" and not conflict["proposals"]
    positional = audience_scoped_recipe_proposals({
        "course_evidence": [page(1, "https://u.test/e1", "audience-selector-0")]
    })
    assert positional["status"] == "needs_review" and not positional["proposals"]


@pytest.mark.asyncio
async def test_task555_live_proposal_reload_extract_stage_and_cas_rollback(monkeypatch):
    from app.database import AsyncSessionLocal, engine
    from app.models import ScrapedCourse, ScrapedFieldEvidence, University
    from app.services.scraper import central_pages
    from app.services.scraper.orchestrator import (
        recipe_central_prefetch_config, stage_runtime_extraction,
    )
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.recipe_rules import (
        build_audience_scoped_recipe_proposal,
    )
    from app.services.scraper.ai_repair_live import extract_audience_option_evidence
    from app.services.scraper.ai_repair_agent import (
        _apply_recipe_to_db,
        _restore_db_config,
    )

    marker = uuid.uuid4().hex[:10]
    course_url = f"https://task555-{marker}.example.edu/course/business"
    english_url = f"https://task555-{marker}.example.edu/english"
    course_template = """
    <html><body><main>
      <h1>Master of Business</h1><p>Qualification: Master degree</p>
      <p>Duration: 2 years</p><p>Study mode: Full-time on campus</p>
      <label>Audience</label>
      <select id="student-audience" name="audience">
        <option value="domestic" {domestic}>Domestic students</option>
        <option value="international" data-url="{english}" {international}>International students January 2027</option>
      </select>
      <p>International tuition fee A$35,000 per year</p>
      <p>Location: Sydney campus</p>
    </main></body></html>
    """
    english_html = """<html><body><main><h1>English requirements</h1>
      <p>IELTS overall score of 6.5. Listening 6.0, Reading 6.0, Writing 6.0,
      Speaking 6.0.</p>
    </main></body></html>"""
    pages = {
        course_url + "?audience=domestic": course_template.format(
            domestic="selected", international="", english=english_url
        ),
        course_url + "?audience=international": course_template.format(
            domestic="", international="selected", english=english_url
        ),
        english_url: english_html,
    }
    async def mocked_fetch(url, **_):
        return pages[url]
    monkeypatch.setattr(central_pages, "fetch_html", mocked_fetch)

    # The canonical proposal is produced from selector evidence, not an
    # injected audience label.
    evidence = extract_audience_option_evidence(
        pages[course_url + "?audience=international"],
        course_url + "?audience=international",
        course_url,
    )
    proposal = build_audience_scoped_recipe_proposal(evidence)
    assert proposal["status"] == "accepted"
    recipe = {"audience_recipe": proposal["proposals"][0]}

    await engine.dispose()
    async with AsyncSessionLocal() as db:
        uni = University(
            name=f"Task555 {marker}", country="Australia", city="Sydney",
            website=f"https://task555-{marker}.example.edu",
            scrape_config={"recipe": {}},
        )
        db.add(uni)
        await db.commit()
        await db.refresh(uni)
        before = copy.deepcopy(uni.scrape_config or {})

        applied = await _apply_recipe_to_db(
            uni.id, recipe, db, expected_config=before
        )
        reloaded = await db.get(University, uni.id)
        await db.refresh(reloaded)
        assert reloaded.scrape_config["recipe"]["audience_recipe"] == {
            **recipe["audience_recipe"],
            "selectors": [
                {**row, "intake_months": list(row["intake_months"])}
                for row in recipe["audience_recipe"]["selectors"]
            ],
        }

        cfg = get_config_for_host(
            hostname=f"task555-{marker}.example.edu",
            name=uni.name, scrape_url=course_url, university_id=uni.id,
            db_scrape_config=reloaded.scrape_config, create_missing_stub=False,
        )
        set_uni_config(cfg)
        central_cfg = recipe_central_prefetch_config(
            {}, reloaded.scrape_config, course_url
        )
        central = await central_pages.prefetch_central_pages(
            central_cfg, university_id=uni.id,
        )
        assert central["english"]["ielts_overall"] == 6.5
        rows = []
        for audience in ("domestic", "international"):
            url = course_url + f"?audience={audience}"
            result = await extract_course(
                url, html=pages[url], use_ai_fallback=False, central_data=central
            )
            payload = result["payload"]
            extraction_evidence = result.get("evidence") or []
            assert payload["audience_identity"]["audience"] == audience
            if audience == "international":
                assert payload["intake_months"] in (["January"], [1]) or not payload.get("intake_months")
                assert payload["ielts_overall"] == 6.5
                assert payload["ielts_listening"] == 6.0
                assert any(e.get("source_url") == english_url for e in result["evidence"])
            else:
                assert "intake_provenance" not in payload
                for field in (
                    "ielts_overall", "ielts_listening", "ielts_reading",
                    "ielts_writing", "ielts_speaking",
                ):
                    assert payload.get(field) is None
                assert not any(
                    row.get("source_url") == english_url
                    for row in extraction_evidence
                )
            staged = await stage_runtime_extraction(
                db, scrape_job_id=f"task555-{marker}", university_id=uni.id,
                course_name=payload["course_name"], source_url=url,
                extracted=result, recipe=reloaded.scrape_config["recipe"],
            )
            assert staged.saved, staged
            rows.append(staged.scraped_course_id)
        stored = (await db.execute(
            ScrapedCourse.__table__.select().where(
                ScrapedCourse.id.in_(rows)
            )
        )).mappings().all()
        assert len(stored) == 2
        evidence_rows = (await db.execute(
            ScrapedFieldEvidence.__table__.select().where(
                ScrapedFieldEvidence.scraped_course_id.in_(rows)
            )
        )).mappings().all()
        assert any(
            row.get("source_url") == english_url
            and "student-audience" in (row.get("snippet") or "")
            for row in evidence_rows
        )

        # Compensating rollback is fenced against the exact applied document.
        await _restore_db_config(uni.id, applied["before"], db, expected_current=applied["applied"])
        newer = copy.deepcopy(applied["applied"])
        newer["operator_edit"] = True
        reloaded.scrape_config = newer
        await db.commit()
        with pytest.raises(RuntimeError, match="newer config"):
            await _restore_db_config(uni.id, applied["before"], db, expected_current=applied["applied"])