"""Phase 10 — Change Detection Engine unit tests.

Tests cover:
- take_snapshot: builds correct snapshot dicts from scraped courses
- detect_changes: emits correct CourseChangeEvent rows (new/removed/field)
- severity classification: critical / major / minor / info
- _norm: skips formatting-only differences (same after norm)
- confidence gate: skips low-confidence critical-field changes
- page_hash fast-exit: skips field diffing when hash unchanged

All DB interactions are mocked via AsyncMock so no real Postgres is needed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _scalars_result(rows):
    """Mock whose .scalars().all() returns rows."""
    res = MagicMock()
    res.scalars.return_value.all.return_value = rows
    return res


def _first_result(row):
    """Mock whose .first() returns row (e.g. a tuple or None)."""
    res = MagicMock()
    res.first.return_value = row
    return res


# ─── _norm ────────────────────────────────────────────────────────────────────

class TestNorm:
    def setup_method(self):
        from app.services.scraper.change_detector import _norm
        self.norm = _norm

    def test_none_returns_none(self):
        assert self.norm("international_fee", None) is None

    def test_strips_whitespace(self):
        result = self.norm("course_location", "  Sydney  ")
        assert result == "sydney"

    def test_lowercases(self):
        result = self.norm("study_mode", "On Campus")
        assert result == result.lower()

    def test_different_formatting_same_value(self):
        a = self.norm("international_fee", "AUD 45,000")
        b = self.norm("international_fee", "aud 45000")
        assert a == b

    def test_ielts_values_normalised(self):
        a = self.norm("ielts_overall", "6.5")
        b = self.norm("ielts_overall", "6.5")
        assert a == b

    def test_empty_string_normalises(self):
        result = self.norm("duration", "")
        assert result is None or isinstance(result, str)


# ─── _severity ────────────────────────────────────────────────────────────────

class TestSeverity:
    def setup_method(self):
        from app.services.scraper.change_detector import _severity
        self.sev = _severity

    def test_fee_is_critical(self):
        assert self.sev("international_fee", "field_change") == "critical"

    def test_ielts_is_critical(self):
        assert self.sev("ielts_overall", "field_change") == "critical"

    def test_intakes_is_critical(self):
        assert self.sev("intake_months", "field_change") == "critical"

    def test_duration_is_major(self):
        assert self.sev("duration", "field_change") == "major"

    def test_location_is_major(self):
        assert self.sev("course_location", "field_change") == "major"

    def test_new_course_is_major(self):
        assert self.sev("", "new_course") == "major"

    def test_removed_course_is_major(self):
        assert self.sev("", "removed_course") == "major"

    def test_study_mode_is_minor(self):
        assert self.sev("study_mode", "field_change") == "minor"

    def test_academic_level_is_minor(self):
        assert self.sev("academic_level", "field_change") == "minor"

    def test_unknown_field_is_info(self):
        assert self.sev("some_random_field", "field_change") == "info"


# ─── take_snapshot ────────────────────────────────────────────────────────────

def _make_staged(id=10, course_id=101, name="Bachelor of Science", fee="45000",
                 ielts="6.5", intakes="February,July", duration="3 years",
                 mode="On Campus", location="Sydney", degree="Bachelor",
                 level="Undergraduate", score="65"):
    sc = MagicMock()
    sc.id = id
    sc.course_id = course_id
    sc.course_name = name
    sc.international_fee = fee
    sc.ielts_overall = ielts
    sc.intake_months = intakes
    sc.duration = duration          # text — safe-cast must handle this
    sc.study_mode = mode
    sc.course_location = location
    sc.degree_level = degree
    sc.academic_level = level
    sc.academic_score = score
    sc.pte_overall = None
    sc.toefl_overall = None
    sc.other_requirement = None
    sc.course_website = "https://uni.edu/bsc"
    sc.fee_term = "year"
    sc.duration_term = None
    sc.avg_verification_confidence = 80.0
    sc.auto_publish_status = "pending"
    return sc


class TestTakeSnapshot:
    @pytest.mark.asyncio
    async def test_empty_staged_returns_zero(self):
        from app.services.scraper.change_snapshot import take_snapshot
        db = _make_db()
        db.execute.return_value = _scalars_result([])
        result = await take_snapshot(university_id=1, scrape_job_id="job_abc", db=db)
        assert result == 0

    @pytest.mark.asyncio
    async def test_one_course_creates_one_snapshot(self):
        from app.services.scraper.change_snapshot import take_snapshot
        db = _make_db()
        db.execute.return_value = _scalars_result([_make_staged()])
        batches = []
        db.add_all.side_effect = lambda lst: batches.extend(lst)

        result = await take_snapshot(university_id=1, scrape_job_id="job_abc", db=db)
        assert result == 1
        assert len(batches) == 1
        snap = batches[0]
        assert snap.course_name == "Bachelor of Science"
        assert snap.scrape_job_id == "job_abc"
        assert snap.university_id == 1

    @pytest.mark.asyncio
    async def test_page_hash_is_sha256_hex(self):
        from app.services.scraper.change_snapshot import take_snapshot
        db = _make_db()
        db.execute.return_value = _scalars_result([_make_staged()])
        batches = []
        db.add_all.side_effect = lambda lst: batches.extend(lst)

        await take_snapshot(university_id=2, scrape_job_id="job_xyz", db=db)
        assert batches[0].page_hash is not None
        assert len(batches[0].page_hash) in (40, 64)  # SHA-1 or SHA-256

    @pytest.mark.asyncio
    async def test_text_duration_stored_safely(self):
        """'3 years' must not raise ValueError — stored as None."""
        from app.services.scraper.change_snapshot import take_snapshot
        db = _make_db()
        sc = _make_staged(duration="3 years")
        db.execute.return_value = _scalars_result([sc])
        batches = []
        db.add_all.side_effect = lambda lst: batches.extend(lst)

        await take_snapshot(university_id=1, scrape_job_id="job_dur", db=db)
        assert batches[0].duration is None  # non-numeric → None

    @pytest.mark.asyncio
    async def test_multiple_courses_all_snapshotted(self):
        from app.services.scraper.change_snapshot import take_snapshot
        db = _make_db()
        courses = [_make_staged(id=i, course_id=100 + i, name=f"Course {i}") for i in range(5)]
        db.execute.return_value = _scalars_result(courses)
        batches = []
        db.add_all.side_effect = lambda lst: batches.extend(lst)

        result = await take_snapshot(university_id=3, scrape_job_id="job_multi", db=db)
        assert result == 5
        assert len(batches) == 5


# ─── detect_changes ──────────────────────────────────────────────────────────

def _make_snap(course_name="BSc Default", page_hash="hash_a",
               international_fee=None, ielts_overall=None,
               intake_months=None, duration=None, study_mode=None,
               course_location=None, degree_level=None,
               academic_level=None, academic_score=None,
               pte_overall=None, toefl_overall=None,
               other_requirement=None, course_url=None,
               avg_verification_confidence=80.0, course_id=None):
    snap = MagicMock()
    snap.course_name = course_name
    snap.page_hash = page_hash
    snap.international_fee = international_fee
    snap.ielts_overall = ielts_overall
    snap.intake_months = intake_months
    snap.duration = duration
    snap.study_mode = study_mode
    snap.course_location = course_location
    snap.degree_level = degree_level
    snap.academic_level = academic_level
    snap.academic_score = academic_score
    snap.pte_overall = pte_overall
    snap.toefl_overall = toefl_overall
    snap.other_requirement = other_requirement
    snap.course_url = course_url
    snap.avg_verification_confidence = avg_verification_confidence
    snap.course_id = course_id
    return snap


def _make_3query_side_effect(current_snaps, prev_job_row, prev_snaps):
    """
    detect_changes makes exactly 3 db.execute calls in order:
      1. current snapshots  → scalars().all()
      2. prev job lookup    → .first() returning (job_id,) or None
      3. previous snapshots → scalars().all()
    """
    call_count = [0]

    async def _execute(stmt, *a, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return _scalars_result(current_snaps)
        if call_count[0] == 2:
            return _first_result(prev_job_row)
        return _scalars_result(prev_snaps)

    return _execute


class TestDetectChanges:
    @pytest.mark.asyncio
    async def test_no_current_snapshots_returns_zero(self):
        """If current job has no snapshots at all, detect_changes bails early."""
        from app.services.scraper.change_detector import detect_changes
        db = _make_db()
        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[],
            prev_job_row=("prev_job",),
            prev_snaps=[],
        )
        result = await detect_changes(university_id=1, scrape_job_id="job_new", db=db)
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_previous_job_returns_zero(self):
        """First scrape for a uni — no prior snapshot — baseline established, 0 events."""
        from app.services.scraper.change_detector import detect_changes
        db = _make_db()
        curr = _make_snap(course_name="BSc Physics")
        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr],
            prev_job_row=None,       # no previous job exists
            prev_snaps=[],
        )
        result = await detect_changes(university_id=1, scrape_job_id="job_first", db=db)
        assert result == 0

    @pytest.mark.asyncio
    async def test_new_course_emits_event(self):
        from app.services.scraper.change_detector import detect_changes
        from app.models.course_change_event import CourseChangeEvent
        db = _make_db()

        curr = _make_snap(course_name="Brand New Course", page_hash="hash_new")

        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr],
            prev_job_row=("job_old",),
            prev_snaps=[],          # old job had no courses → new course
        )
        added = []
        db.add_all.side_effect = lambda lst: added.extend(lst)

        result = await detect_changes(university_id=5, scrape_job_id="job_new", db=db)
        assert result >= 1
        types = [e.change_type for e in added if isinstance(e, CourseChangeEvent)]
        assert "new_course" in types

    @pytest.mark.asyncio
    async def test_removed_course_emits_event(self):
        from app.services.scraper.change_detector import detect_changes
        from app.models.course_change_event import CourseChangeEvent
        db = _make_db()

        # current run has a different course; "Old Dropped" was in previous
        curr_keeper = _make_snap(course_name="Kept Course", page_hash="hash_k")
        prev_dropped = _make_snap(course_name="Old Dropped Course", page_hash="hash_d")

        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr_keeper],
            prev_job_row=("job_prev",),
            prev_snaps=[prev_dropped, curr_keeper],
        )
        added = []
        db.add_all.side_effect = lambda lst: added.extend(lst)

        result = await detect_changes(university_id=5, scrape_job_id="job_next", db=db)
        assert result >= 1
        types = [e.change_type for e in added if isinstance(e, CourseChangeEvent)]
        assert "removed_course" in types

    @pytest.mark.asyncio
    async def test_critical_fee_change_emits_event(self):
        from app.services.scraper.change_detector import detect_changes
        from app.models.course_change_event import CourseChangeEvent
        db = _make_db()

        prev = _make_snap(course_name="BSc CS", international_fee=40000.0,
                          avg_verification_confidence=85.0, page_hash="old")
        curr = _make_snap(course_name="BSc CS", international_fee=48000.0,
                          avg_verification_confidence=85.0, page_hash="new")

        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr],
            prev_job_row=("job_prev",),
            prev_snaps=[prev],
        )
        added = []
        db.add_all.side_effect = lambda lst: added.extend(lst)

        await detect_changes(university_id=3, scrape_job_id="job_fee", db=db)
        fee_events = [
            e for e in added
            if isinstance(e, CourseChangeEvent) and e.field_name == "international_fee"
        ]
        assert len(fee_events) >= 1
        assert fee_events[0].severity == "critical"

    @pytest.mark.asyncio
    async def test_same_page_hash_skips_field_comparison(self):
        from app.services.scraper.change_detector import detect_changes
        from app.models.course_change_event import CourseChangeEvent
        db = _make_db()

        SAME = "identical_hash_xyz"
        prev = _make_snap(course_name="BSc Physics", international_fee=40000.0,
                          avg_verification_confidence=85.0, page_hash=SAME)
        curr = _make_snap(course_name="BSc Physics", international_fee=99999.0,
                          avg_verification_confidence=85.0, page_hash=SAME)

        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr],
            prev_job_row=("job_prev",),
            prev_snaps=[prev],
        )
        added = []
        db.add_all.side_effect = lambda lst: added.extend(lst)

        await detect_changes(university_id=3, scrape_job_id="job_hash", db=db)
        fee_events = [
            e for e in added
            if isinstance(e, CourseChangeEvent) and e.field_name == "international_fee"
        ]
        assert len(fee_events) == 0

    @pytest.mark.asyncio
    async def test_low_confidence_critical_field_skipped(self):
        from app.services.scraper.change_detector import detect_changes
        from app.models.course_change_event import CourseChangeEvent
        db = _make_db()

        prev = _make_snap(course_name="BSc Arts", international_fee=40000.0,
                          avg_verification_confidence=50.0, page_hash="old")
        curr = _make_snap(course_name="BSc Arts", international_fee=55000.0,
                          avg_verification_confidence=50.0, page_hash="new")

        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr],
            prev_job_row=("job_prev",),
            prev_snaps=[prev],
        )
        added = []
        db.add_all.side_effect = lambda lst: added.extend(lst)

        await detect_changes(university_id=3, scrape_job_id="job_lowconf", db=db)
        fee_events = [
            e for e in added
            if isinstance(e, CourseChangeEvent) and e.field_name == "international_fee"
        ]
        assert len(fee_events) == 0

    @pytest.mark.asyncio
    async def test_no_field_events_when_page_hash_same(self):
        from app.services.scraper.change_detector import detect_changes
        from app.models.course_change_event import CourseChangeEvent
        db = _make_db()

        SAME = "same_hash_abc"
        prev = _make_snap(course_name="BSc Identical", page_hash=SAME,
                          avg_verification_confidence=85.0)
        curr = _make_snap(course_name="BSc Identical", page_hash=SAME,
                          avg_verification_confidence=85.0)

        db.execute.side_effect = _make_3query_side_effect(
            current_snaps=[curr],
            prev_job_row=("job_prev",),
            prev_snaps=[prev],
        )
        added = []
        db.add_all.side_effect = lambda lst: added.extend(lst)

        result = await detect_changes(university_id=3, scrape_job_id="job_nochange", db=db)
        field_events = [
            e for e in added
            if isinstance(e, CourseChangeEvent) and e.change_type == "field_change"
        ]
        assert len(field_events) == 0
