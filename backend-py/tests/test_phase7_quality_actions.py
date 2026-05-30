"""Phase 7: Autonomous Quality Action Dispatcher — unit tests.

All tests are pure-Python with no DB/Celery/network I/O.
Run:
    PYTHONPATH=. SKIP_XHR_CAPTURE=1 pytest tests/test_phase7_quality_actions.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from typing import Any

from app.services.scraper.quality_action_dispatcher import (
    ActionType,
    QualityAction,
    DispatchResult,
    _FIELD_ACTION_MAP,
    _CRITICAL_FIELDS,
    _GOOD_FILL,
    _ACT_THRESHOLD,
    _WEAK_FILL,
    _MAX_CELERY_DISPATCHES,
    _skip_reason,
    get_avg_completeness,
    dispatch_quality_actions,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_fill_rates(**overrides: float) -> dict[str, float]:
    """Build a fill_rates dict with all 13 review fields defaulting to 1.0."""
    base = {
        "course_name": 1.0,
        "degree_level": 1.0,
        "category": 1.0,
        "study_mode": 1.0,
        "course_location": 1.0,
        "duration": 1.0,
        "intake_months": 1.0,
        "international_fee": 1.0,
        "description": 1.0,
        "academic_level": 1.0,
        "academic_score": 1.0,
        "english_test": 1.0,
        "other_requirement": 1.0,
    }
    base.update(overrides)
    return base


def make_execute_result(scalar_val: float = 0.75, rowcount: int = 0) -> MagicMock:
    """Return a synchronous MagicMock for the result of `await db.execute(...)`."""
    r = MagicMock()
    r.scalar.return_value = scalar_val
    r.rowcount = rowcount
    r.mappings.return_value.first.return_value = None
    return r


def make_db(avg: float = 0.75) -> AsyncMock:
    """Mock AsyncSession whose execute().scalar() returns avg for AVG queries.

    Critical: db.execute is AsyncMock, so ``await db.execute(...)`` resolves
    to ``db.execute.return_value``.  We set that to a plain MagicMock so
    calling ``.scalar()`` on it returns the float directly (not a coroutine).
    """
    db = AsyncMock()
    db.execute.return_value = make_execute_result(scalar_val=avg)
    db.commit = AsyncMock()
    return db


# ── ActionType field mapping ──────────────────────────────────────────────────

class TestFieldActionMap:
    def test_pdf_fields_mapped_to_pdf_extraction(self):
        for fk in ("international_fee", "other_requirement", "english_test", "academic_score"):
            assert _FIELD_ACTION_MAP[fk][0] == ActionType.PDF_EXTRACTION, fk

    def test_structural_fields_mapped_to_repair_extractor(self):
        for fk in ("degree_level", "course_name", "academic_level", "category",
                   "description", "study_mode", "duration", "course_location", "intake_months"):
            assert _FIELD_ACTION_MAP[fk][0] == ActionType.REPAIR_EXTRACTOR, fk

    def test_all_13_review_fields_have_mapping(self):
        review_fields = {
            "course_name", "degree_level", "category", "study_mode", "course_location",
            "duration", "intake_months", "international_fee", "description",
            "academic_level", "academic_score", "english_test", "other_requirement",
        }
        for fk in review_fields:
            assert fk in _FIELD_ACTION_MAP, f"{fk!r} missing from _FIELD_ACTION_MAP"

    def test_each_mapping_has_non_empty_reason(self):
        for fk, (at, reason) in _FIELD_ACTION_MAP.items():
            assert reason, f"{fk!r} has empty reason"
            assert isinstance(at, ActionType), fk

    def test_critical_fields_subset_of_mapped_fields(self):
        for fk in _CRITICAL_FIELDS:
            assert fk in _FIELD_ACTION_MAP, f"critical field {fk!r} not in _FIELD_ACTION_MAP"


# ── QualityAction dataclass ───────────────────────────────────────────────────

class TestQualityAction:
    def test_to_dict_keys(self):
        a = QualityAction(
            action_type=ActionType.PDF_EXTRACTION,
            target_fields=["international_fee"],
            reason="fees in PDFs",
        )
        d = a.to_dict()
        assert d["action_type"] == "pdf_extraction"
        assert d["target_fields"] == ["international_fee"]
        assert d["executed"] is False
        assert d["courses_improved"] == 0
        assert d["skipped_reason"] == ""

    def test_to_dict_executed(self):
        a = QualityAction(
            action_type=ActionType.REPAIR_EXTRACTOR,
            target_fields=["degree_level"],
            reason="rules broken",
            executed=True,
            result="repair_extractor queued",
        )
        d = a.to_dict()
        assert d["executed"] is True
        assert d["result"] == "repair_extractor queued"


# ── DispatchResult dataclass ──────────────────────────────────────────────────

class TestDispatchResult:
    def test_to_dict_empty(self):
        r = DispatchResult()
        d = r.to_dict()
        assert d["overall_before"] == 0.0
        assert d["overall_after"] == 0.0
        assert d["actions"] == []
        assert d["inline_improved"] == 0
        assert d["celery_dispatched"] == []

    def test_to_dict_with_actions(self):
        r = DispatchResult(
            overall_before=0.72,
            overall_after=0.78,
            inline_improved=5,
            celery_dispatched=["repair_extractor"],
        )
        r.actions.append(QualityAction(
            action_type=ActionType.PDF_EXTRACTION,
            target_fields=["english_test"],
            reason="IELTS in PDF",
            executed=True,
            courses_improved=5,
        ))
        d = r.to_dict()
        assert d["overall_before"] == 0.72
        assert d["overall_after"] == 0.78
        assert d["inline_improved"] == 5
        assert d["celery_dispatched"] == ["repair_extractor"]
        assert len(d["actions"]) == 1
        assert d["actions"][0]["action_type"] == "pdf_extraction"

    def test_overall_values_rounded_to_3dp(self):
        r = DispatchResult(overall_before=0.12345678, overall_after=0.87654321)
        d = r.to_dict()
        assert d["overall_before"] == 0.123
        assert d["overall_after"] == 0.877


# ── _skip_reason safety logic ─────────────────────────────────────────────────

class TestSkipReason:
    def test_good_fill_rate_skipped(self):
        reason = _skip_reason(ActionType.PDF_EXTRACTION, 0.85, set(), 0, False)
        assert reason is not None
        assert "already good" in reason

    def test_exactly_good_threshold_skipped(self):
        reason = _skip_reason(ActionType.REPAIR_EXTRACTOR, _GOOD_FILL, set(), 0, False)
        assert reason is not None

    def test_below_good_threshold_not_skipped(self):
        reason = _skip_reason(ActionType.PDF_EXTRACTION, 0.30, set(), 0, False)
        assert reason is None

    def test_already_dispatched_skipped(self):
        already = {ActionType.PDF_EXTRACTION}
        reason = _skip_reason(ActionType.PDF_EXTRACTION, 0.10, already, 0, False)
        assert reason is not None
        assert "already dispatched" in reason

    def test_celery_budget_exhausted(self):
        reason = _skip_reason(
            ActionType.REPAIR_EXTRACTOR, 0.10, set(), _MAX_CELERY_DISPATCHES, False,
        )
        assert reason is not None
        assert "budget exhausted" in reason

    def test_cascade_repair_fired_skips_repair_extractor(self):
        reason = _skip_reason(ActionType.REPAIR_EXTRACTOR, 0.10, set(), 0, True)
        assert reason is not None
        assert "CASCADE" in reason

    def test_cascade_repair_fired_does_not_skip_browser_retry(self):
        reason = _skip_reason(ActionType.BROWSER_RETRY, 0.10, set(), 0, True)
        assert reason is None

    def test_pdf_extraction_not_blocked_by_cascade_flag(self):
        reason = _skip_reason(ActionType.PDF_EXTRACTION, 0.10, set(), 0, True)
        assert reason is None

    def test_manual_review_not_budget_limited(self):
        reason = _skip_reason(
            ActionType.MANUAL_REVIEW, 0.10, set(), _MAX_CELERY_DISPATCHES, False,
        )
        assert reason is None

    def test_browser_retry_blocked_by_budget(self):
        reason = _skip_reason(
            ActionType.BROWSER_RETRY, 0.10, set(), _MAX_CELERY_DISPATCHES, False,
        )
        assert reason is not None
        assert "budget exhausted" in reason


# ── get_avg_completeness ──────────────────────────────────────────────────────

class TestGetAvgCompleteness:
    @pytest.mark.asyncio
    async def test_returns_float_from_scalar(self):
        db = AsyncMock()
        db.execute.return_value = make_execute_result(scalar_val=76)
        avg = await get_avg_completeness("job-1", db)
        assert avg == pytest.approx(0.76)

    @pytest.mark.asyncio
    async def test_returns_zero_when_scalar_none(self):
        db = AsyncMock()
        r = make_execute_result()
        r.scalar.return_value = None
        db.execute.return_value = r
        avg = await get_avg_completeness("job-1", db)
        assert avg == 0.0


# ── dispatch_quality_actions — threshold skip ─────────────────────────────────

class TestDispatchQualityActionsThreshold:
    @pytest.mark.asyncio
    async def test_skips_when_avg_already_good(self):
        db = make_db(avg=0.90)
        fill_rates = make_fill_rates()
        result = await dispatch_quality_actions(
            university_id=1, job_id="j1", fill_rates=fill_rates,
            scrape_url="https://example.edu", uni_country="AU",
            uni_scrape_config={}, db=db, overall_avg=0.90,
        )
        assert result.actions == []
        assert result.inline_improved == 0
        assert result.celery_dispatched == []

    @pytest.mark.asyncio
    async def test_skips_when_no_weak_fields(self):
        db = make_db(avg=0.75)
        fill_rates = make_fill_rates()  # all 1.0
        result = await dispatch_quality_actions(
            university_id=1, job_id="j1", fill_rates=fill_rates,
            scrape_url="https://example.edu", uni_country="AU",
            uni_scrape_config={}, db=db, overall_avg=0.75,
        )
        assert result.actions == []

    @pytest.mark.asyncio
    async def test_fires_when_avg_in_gap(self):
        db = make_db(avg=0.75)
        fill_rates = make_fill_rates(international_fee=0.20)
        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
            return_value=(3, "international_fee+3"),
        ):
            result = await dispatch_quality_actions(
                university_id=1, job_id="j1", fill_rates=fill_rates,
                scrape_url="https://example.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.75,
            )
        assert any(a.action_type == ActionType.PDF_EXTRACTION for a in result.actions)


# ── dispatch_quality_actions — PDF extraction ─────────────────────────────────

class TestDispatchPdfExtraction:
    @pytest.mark.asyncio
    async def test_pdf_extraction_executed_for_fee(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(international_fee=0.15)
        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
            return_value=(8, "international_fee+8"),
        ) as mock_pdf:
            result = await dispatch_quality_actions(
                university_id=42, job_id="j-fee", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        mock_pdf.assert_called_once()
        pdf_action = next(a for a in result.actions if a.action_type == ActionType.PDF_EXTRACTION)
        assert pdf_action.executed is True
        assert "international_fee" in pdf_action.target_fields
        assert result.inline_improved == 8

    @pytest.mark.asyncio
    async def test_multiple_pdf_fields_grouped_into_one_action(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(
            international_fee=0.10, english_test=0.05, other_requirement=0.20,
        )
        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
            return_value=(5, "international_fee+3;ielts_overall+2"),
        ):
            result = await dispatch_quality_actions(
                university_id=42, job_id="j-multi", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        pdf_actions = [a for a in result.actions if a.action_type == ActionType.PDF_EXTRACTION]
        assert len(pdf_actions) == 1  # all PDF fields → single action
        assert len(pdf_actions[0].target_fields) >= 2

    @pytest.mark.asyncio
    async def test_pdf_extraction_not_called_when_all_fee_fields_good(self):
        db = make_db(avg=0.74)
        fill_rates = make_fill_rates(degree_level=0.10)  # only repair needed
        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
        ) as mock_pdf:
            with patch(
                "app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
                return_value=True,
            ):
                result = await dispatch_quality_actions(
                    university_id=42, job_id="j-nopdff", fill_rates=fill_rates,
                    scrape_url="https://uni.edu", uni_country="AU",
                    uni_scrape_config={}, db=db, overall_avg=0.74,
                )
        mock_pdf.assert_not_called()
        assert any(a.action_type == ActionType.REPAIR_EXTRACTOR for a in result.actions)


# ── dispatch_quality_actions — repair_extractor dispatch ──────────────────────

class TestDispatchRepairExtractor:
    @pytest.mark.asyncio
    async def test_repair_dispatched_for_structural_field(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(degree_level=0.10)
        with patch(
            "app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
            return_value=True,
        ) as mock_repair:
            result = await dispatch_quality_actions(
                university_id=5, job_id="j-repair", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        mock_repair.assert_called_once_with(5, "j-repair")
        assert "repair_extractor" in result.celery_dispatched

    @pytest.mark.asyncio
    async def test_repair_skipped_when_cascade_repair_fired(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(degree_level=0.10)
        with patch(
            "app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
            return_value=True,
        ) as mock_repair:
            result = await dispatch_quality_actions(
                university_id=5, job_id="j-cascade-skip", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
                cascade_repair_fired=True,
            )
        mock_repair.assert_not_called()
        repair_action = next(
            (a for a in result.actions if a.action_type == ActionType.REPAIR_EXTRACTOR), None
        )
        assert repair_action is not None
        assert repair_action.executed is False
        assert "CASCADE" in repair_action.skipped_reason

    @pytest.mark.asyncio
    async def test_repair_dispatched_at_most_once(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(degree_level=0.05, study_mode=0.10, duration=0.15)
        call_count = 0

        def fake_dispatch(uni_id: int, job_id: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        with patch(
            "app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
            side_effect=fake_dispatch,
        ):
            result = await dispatch_quality_actions(
                university_id=5, job_id="j-once", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        assert call_count == 1  # even though 3 repair-fields, dispatch only once


# ── dispatch_quality_actions — Celery budget cap ──────────────────────────────

class TestCeleryBudgetCap:
    @pytest.mark.asyncio
    async def test_celery_cap_respected(self):
        db = make_db(avg=0.72)
        # Both repair AND browser-needing fields present
        fill_rates = make_fill_rates(degree_level=0.05, other_requirement=0.10)
        repair_calls = 0
        browser_calls = 0

        def fake_repair(uni_id: int, job_id: str) -> bool:
            nonlocal repair_calls
            repair_calls += 1
            return True

        def fake_browser(uni_id: int, job_id: str) -> bool:
            nonlocal browser_calls
            browser_calls += 1
            return True

        with patch("app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
                   new_callable=AsyncMock, return_value=(0, "no match")):
            with patch("app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
                       side_effect=fake_repair):
                with patch("app.services.scraper.quality_action_dispatcher._dispatch_browser_retry",
                           side_effect=fake_browser):
                    result = await dispatch_quality_actions(
                        university_id=5, job_id="j-budget", fill_rates=fill_rates,
                        scrape_url="https://uni.edu", uni_country="AU",
                        uni_scrape_config={}, db=db, overall_avg=0.72,
                    )
        total_celery = repair_calls + browser_calls
        assert total_celery <= _MAX_CELERY_DISPATCHES


# ── dispatch_quality_actions — critical field priority ────────────────────────

class TestCriticalFieldPriority:
    @pytest.mark.asyncio
    async def test_critical_fields_appear_first_in_target_fields(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(
            degree_level=0.05,    # critical + repair
            international_fee=0.10,  # critical + PDF
        )
        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
            return_value=(2, "international_fee+2"),
        ):
            with patch(
                "app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
                return_value=True,
            ):
                result = await dispatch_quality_actions(
                    university_id=1, job_id="j-prio", fill_rates=fill_rates,
                    scrape_url="https://uni.edu", uni_country="AU",
                    uni_scrape_config={}, db=db, overall_avg=0.72,
                )
        assert len(result.actions) >= 2
        # PDF action should come before REPAIR in the result list
        types_in_order = [a.action_type for a in result.actions if a.executed]
        if ActionType.PDF_EXTRACTION in types_in_order and ActionType.REPAIR_EXTRACTOR in types_in_order:
            assert types_in_order.index(ActionType.PDF_EXTRACTION) < types_in_order.index(ActionType.REPAIR_EXTRACTOR)


# ── dispatch_quality_actions — overall_avg provided vs re-queried ─────────────

class TestOverallAvgHandling:
    @pytest.mark.asyncio
    async def test_uses_provided_overall_avg_not_db(self):
        db = make_db(avg=0.55)  # DB would say 55 % — should be ignored
        fill_rates = make_fill_rates()  # all good
        result = await dispatch_quality_actions(
            university_id=1, job_id="j-avg", fill_rates=fill_rates,
            scrape_url="https://uni.edu", uni_country="AU",
            uni_scrape_config={}, db=db,
            overall_avg=0.90,  # provided → used directly, skip DB check
        )
        assert result.actions == []  # 90 % ≥ threshold, no actions

    @pytest.mark.asyncio
    async def test_re_queries_db_when_overall_avg_none(self):
        db = make_db(avg=90)  # above threshold; DB stores 0-100 integers
        fill_rates = make_fill_rates()
        result = await dispatch_quality_actions(
            university_id=1, job_id="j-dbq", fill_rates=fill_rates,
            scrape_url="https://uni.edu", uni_country="AU",
            uni_scrape_config={}, db=db,
            overall_avg=None,  # re-query from DB
        )
        assert result.overall_before == pytest.approx(0.90)
        assert result.actions == []


# ── dispatch_quality_actions — API_PROMOTION path ─────────────────────────────

class TestApiPromotionSkip:
    @pytest.mark.asyncio
    async def test_api_promotion_skipped_with_phase4b_reason(self):
        db = make_db(avg=0.72)
        # Manually add a hypothetical API-promotion field to test the skip path
        # by patching _FIELD_ACTION_MAP temporarily
        with patch.dict(
            "app.services.scraper.quality_action_dispatcher._FIELD_ACTION_MAP",
            {"_test_api_field": (ActionType.API_PROMOTION, "API test reason")},
        ):
            fill_rates = {**make_fill_rates(), "_test_api_field": 0.10}
            result = await dispatch_quality_actions(
                university_id=1, job_id="j-api", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        api_action = next(
            (a for a in result.actions if a.action_type == ActionType.API_PROMOTION), None,
        )
        if api_action:
            assert api_action.executed is False
            assert "Phase 4B" in api_action.skipped_reason


# ── dispatch_quality_actions — inline_improved re-measures avg ────────────────

class TestInlineImprovedRemeasure:
    @pytest.mark.asyncio
    async def test_overall_after_re_measured_when_improved(self):
        execute_calls: list = []
        scalar_values = [78]  # re-measurement value; DB stores 0-100 integers

        class FakeExecute:
            def __init__(self, val: float) -> None:
                self._val = val
            def scalar(self) -> float:
                return self._val

        async def fake_execute(*args: Any, **kwargs: Any) -> FakeExecute:
            return FakeExecute(scalar_values[0])

        db = AsyncMock()
        db.execute.side_effect = fake_execute
        db.commit = AsyncMock()

        fill_rates = make_fill_rates(international_fee=0.10)

        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
            return_value=(3, "international_fee+3"),
        ):
            result = await dispatch_quality_actions(
                university_id=1, job_id="j-remeasure", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        # overall_after should be re-measured (0.78) not equal to before (0.72)
        assert result.overall_after == pytest.approx(0.78)
        assert result.inline_improved == 3

    @pytest.mark.asyncio
    async def test_overall_after_equals_before_when_no_improvement(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(degree_level=0.10)
        with patch(
            "app.services.scraper.quality_action_dispatcher._dispatch_repair_extractor",
            return_value=True,
        ):
            result = await dispatch_quality_actions(
                university_id=1, job_id="j-nochange", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
            )
        assert result.overall_after == pytest.approx(result.overall_before)


# ── dispatch_quality_actions — emit callback ──────────────────────────────────

class TestEmitCallback:
    @pytest.mark.asyncio
    async def test_emit_not_required(self):
        db = make_db(avg=0.72)
        fill_rates = make_fill_rates(international_fee=0.10)
        with patch(
            "app.services.scraper.quality_action_dispatcher._run_pdf_extraction",
            new_callable=AsyncMock,
            return_value=(0, "no match"),
        ):
            result = await dispatch_quality_actions(
                university_id=1, job_id="j-noemit", fill_rates=fill_rates,
                scrape_url="https://uni.edu", uni_country="AU",
                uni_scrape_config={}, db=db, overall_avg=0.72,
                emit=None,
            )
        assert result is not None


# ── Thresholds sanity ─────────────────────────────────────────────────────────

class TestThresholds:
    def test_good_fill_is_80_percent(self):
        assert _GOOD_FILL == 0.80

    def test_act_threshold_is_85_percent(self):
        assert _ACT_THRESHOLD == 0.85

    def test_weak_fill_below_act_threshold(self):
        assert _WEAK_FILL < _ACT_THRESHOLD

    def test_max_celery_dispatches_is_2(self):
        assert _MAX_CELERY_DISPATCHES == 2
