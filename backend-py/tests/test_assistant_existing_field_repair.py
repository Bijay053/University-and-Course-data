"""Production regressions: explicit repairs must not become imports or lose fields."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_field_edits import (
    evidence_edit_fields, evidence_edit_field, field_recheck_options, handle_field_edit,
)

MULTI = (
    "Fix the incorrect international tuition fees, IELTS overall and all four "
    "component scores, and missing intakes for all pending courses in this review. "
    "Check the official course pages and their linked programme requirements. "
    "Keep correct source-backed values unchanged. Do not discover or add courses, approve or publish."
)
FEES = (
    "Repair incorrect international tuition fees for all 49 existing pending courses "
    "in this review using each official course page and its applicable official fee schedule. "
    "Preserve the source fee year, currency and fee basis. Do not discover, add, approve or publish courses."
)


@pytest.mark.parametrize("prompt,expected", [
    (MULTI, {"international_fee", "ielts_overall", "intake_months"}),
    (FEES, {"international_fee"}),
    ("Fix missing fees. Do not add courses.", {"international_fee"}),
    ("Do not import courses; correct the IELTS requirements for all courses.", {"ielts_overall"}),
    ("Repair IELTS overall, Listening, Reading, Writing and Speaking for all courses.", {"ielts_overall"}),
    ("Do not change fees. Fix missing IELTS scores.", {"ielts_overall"}),
    ("Correct duration and campus for all courses.", {"duration", "course_location"}),
])
def test_positive_fields_survive_unrelated_prohibitions(prompt, expected):
    assert evidence_edit_fields(prompt) == expected


@pytest.mark.parametrize("prompt", [
    "Do not repair fees.",
    "Read-only: repair IELTS.",
    "Only explain missing IELTS bands.",
    "Show an approval preview to fix fees.",
    "Add this course https://example.edu/fees.",
    "Discover courses and fix fees.",
    "Repair the fee extractor.",
    "Why are fees missing?",
    "Fix all courses.",
])
def test_research_imports_and_provider_repairs_keep_their_own_boundaries(prompt):
    assert not evidence_edit_fields(prompt)


def test_fee_repair_is_resolved_without_a_model_import_decision():
    assert evidence_edit_field(FEES) == "fee"


def test_intake_shortcut_cannot_discard_other_requested_fields():
    from app.services.assistant_intake_review import requested
    assert not requested(MULTI)
    assert requested("Fix missing intakes for all courses.")


@pytest.mark.asyncio
async def test_full_request_becomes_one_fenced_existing_row_action(monkeypatch):
    from app.services import scrape_assistant as service, assistant_source_cache
    rows = [{"id": n, "name": f"Course {n}", "url": f"https://example.edu/courses/{n}"}
            for n in range(1, 50)]
    ctx = {"samples": rows, "home": "https://example.edu",
           "versions": {str(r["id"]): "original" for r in rows}, "config_version": "config"}
    inspect = AsyncMock(return_value={"verified": True})
    monkeypatch.setattr(assistant_source_cache, "cached_source", inspect)
    save = AsyncMock()
    fake = SimpleNamespace(action_proposal=service.action_proposal, now=service.now,
                           inspect_source=AsyncMock(), save_research_reply=save)
    assert await handle_field_edit(fake, "thread", "turn", "run", ctx, MULTI)
    action = save.call_args.args[3]["actions"][0]
    assert action["kind"] == "fix_fields"
    assert set(action["fields"]) == {"international_fee", "ielts_overall", "intake_months"}
    assert action["_ids"] == list(range(1, 50))
    assert action["_versions"] == ctx["versions"]
    assert action["requiresConfirmation"] is True
    assert "_requestedUrls" not in action and "_values" not in action
    assert inspect.await_count == 3
    options = field_recheck_options(action)
    assert options["forceFields"] == ["intake_months", "international_fee"]
    assert set(options["forceReasons"]) == set(options["forceFields"])


def test_recheck_cannot_force_unselected_or_unsupported_fields():
    assert field_recheck_options({"fields": ["international_fee"],
                                  "_officialFieldRecheck": ["study_mode", "course_name"]}) == {}
    assert field_recheck_options({"fields": ["ielts_overall"],
                                  "_officialFieldRecheck": ["ielts_overall"]}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("host,fields,expected", [
    ("www.cardiffmet.ac.uk", ["international_fee"], True),
    ("www.cardiffmet.ac.uk", ["ielts_overall"], True),
    ("www.cardiffmet.ac.uk", ["intake_months"], False),
    ("www.example.edu", ["international_fee"], False),
    ("www.cardiffmet.ac.uk.evil.test", ["international_fee"], False),
])
async def test_staged_recovery_prefetches_only_applicable_profiles(monkeypatch, host, fields, expected):
    from app.routers.scrape import ReExtractBody, re_extract_staged
    from app.services.scraper import central_pages
    from app.services.scraper.config import loader, context
    from app.services.scraper.extractors import cardiffmet_facts
    cfg = SimpleNamespace(extraction=SimpleNamespace(
        fees=SimpleNamespace(central_page=None, fees_pdf_url=None),
        english=SimpleNamespace(central_page=None, central_page_ug=None, central_page_pg=None)))
    monkeypatch.setattr(loader, "get_config_for_host", lambda **kwargs: cfg)
    monkeypatch.setattr(context, "set_uni_config", lambda value: None)
    monkeypatch.setattr(central_pages, "prefetch_central_pages", AsyncMock(return_value={}))
    profiles = AsyncMock(return_value={cardiffmet_facts.FEES: "official fees",
                                       cardiffmet_facts.PGCE: "PGCE programme"})
    monkeypatch.setattr(cardiffmet_facts, "prefetch_sources", profiles)
    uni = SimpleNamespace(scrape_url=f"https://{host}/", website=f"https://{host}/",
                          name="Test University", country="United Kingdom", scrape_config={})
    db = SimpleNamespace(get=AsyncMock(return_value=uni),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    await re_extract_staged(ReExtractBody(ids=[1], universityId=114, targetFields=fields), db)
    assert profiles.await_count == int(expected)


def test_reextract_passes_the_same_preloaded_profiles_to_each_extraction():
    # The entry-point test above exercises the real setup. Lock the call-site
    # contract too: a successfully prefetched profile must not be dropped.
    path = Path(__file__).parents[1] / "app/routers/scrape.py"
    tree = ast.parse(path.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "re_extract_staged")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_extract_only"]
    assert calls
    assert all(any(k.arg == "uni_pdf_data" and isinstance(k.value, ast.Name)
                   and k.value.id == "recovery_pdf_data" for k in call.keywords) for call in calls)
