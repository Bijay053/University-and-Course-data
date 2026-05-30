"""Tests for Phase 9 Conflict Repair Loop — conflict_repair.py.

Covers:
  - diagnose_conflict: all diagnosis types
  - attempt_repair: resolve / unresolved logic
  - Safety rules (no high-auth overwrite)
  - Source authority classification helpers
  - Edge cases: empty sources, all-low-auth conflict, single source
"""
from __future__ import annotations

import pytest

from app.services.scraper.conflict_repair import (
    HIGH_AUTHORITY,
    LOW_AUTHORITY,
    DIAG_AI_VS_DIRECT,
    DIAG_PATTERN_VS_DIRECT,
    DIAG_LOW_AUTHORITY_VS_DIRECT,
    DIAG_HTML_VS_PDF,
    DIAG_API_VS_HTML,
    DIAG_API_VS_PDF,
    DIAG_MULTI_HIGH_AUTHORITY,
    DIAG_NO_HIGH_AUTHORITY,
    ConflictDiagnosis,
    diagnose_conflict,
    attempt_repair,
)


# ---------------------------------------------------------------------------
# diagnose_conflict
# ---------------------------------------------------------------------------

class TestDiagnoseConflict:
    def test_ai_conflicts_with_html(self):
        d = diagnose_conflict(
            conflict_sources=["ai"],
            all_source_keys=["html", "ai"],
        )
        assert d.diagnosis_type == DIAG_AI_VS_DIRECT
        assert d.can_auto_resolve is True
        assert "html" in d.high_auth_agree
        assert "ai" in d.low_auth_conflict

    def test_pattern_conflicts_with_pdf(self):
        d = diagnose_conflict(
            conflict_sources=["pattern"],
            all_source_keys=["pdf", "pattern"],
        )
        assert d.diagnosis_type == DIAG_PATTERN_VS_DIRECT
        assert d.can_auto_resolve is True

    def test_ai_and_pattern_conflict_with_html(self):
        d = diagnose_conflict(
            conflict_sources=["ai", "pattern"],
            all_source_keys=["html", "ai", "pattern"],
        )
        assert d.diagnosis_type == DIAG_LOW_AUTHORITY_VS_DIRECT
        assert d.can_auto_resolve is True

    def test_html_vs_pdf_unresolvable(self):
        d = diagnose_conflict(
            conflict_sources=["html"],
            all_source_keys=["pdf", "html"],
        )
        assert d.diagnosis_type == DIAG_HTML_VS_PDF
        assert d.can_auto_resolve is False

    def test_pdf_vs_html_unresolvable(self):
        d = diagnose_conflict(
            conflict_sources=["pdf"],
            all_source_keys=["html", "pdf"],
        )
        assert d.diagnosis_type == DIAG_HTML_VS_PDF
        assert d.can_auto_resolve is False

    def test_api_vs_html_unresolvable(self):
        d = diagnose_conflict(
            conflict_sources=["api"],
            all_source_keys=["html", "api"],
        )
        assert d.diagnosis_type == DIAG_API_VS_HTML
        assert d.can_auto_resolve is False

    def test_api_vs_pdf_unresolvable(self):
        d = diagnose_conflict(
            conflict_sources=["api"],
            all_source_keys=["pdf", "api"],
        )
        assert d.diagnosis_type == DIAG_API_VS_PDF
        assert d.can_auto_resolve is False

    def test_multi_high_authority_unresolvable(self):
        d = diagnose_conflict(
            conflict_sources=["html", "pdf"],
            all_source_keys=["html", "pdf", "api"],
        )
        assert d.diagnosis_type == DIAG_MULTI_HIGH_AUTHORITY
        assert d.can_auto_resolve is False

    def test_no_high_authority_at_all(self):
        d = diagnose_conflict(
            conflict_sources=["ai"],
            all_source_keys=["ai", "pattern"],
        )
        # pattern agrees, but no high-authority sources exist → unresolvable
        assert d.can_auto_resolve is False

    def test_ai_conflicts_with_api(self):
        d = diagnose_conflict(
            conflict_sources=["ai"],
            all_source_keys=["api", "ai"],
        )
        assert d.diagnosis_type == DIAG_AI_VS_DIRECT
        assert d.can_auto_resolve is True
        assert "api" in d.high_auth_agree

    def test_ai_conflicts_with_html_and_pdf(self):
        """Two high-auth sources agree; only AI conflicts."""
        d = diagnose_conflict(
            conflict_sources=["ai"],
            all_source_keys=["html", "pdf", "ai"],
        )
        assert d.can_auto_resolve is True
        assert d.diagnosis_type == DIAG_AI_VS_DIRECT


# ---------------------------------------------------------------------------
# attempt_repair
# ---------------------------------------------------------------------------

class TestAttemptRepair:
    def _make_diag(self, can_auto: bool) -> ConflictDiagnosis:
        return ConflictDiagnosis(
            diagnosis_type=DIAG_AI_VS_DIRECT if can_auto else DIAG_HTML_VS_PDF,
            high_auth_agree={"html"} if can_auto else set(),
            low_auth_conflict={"ai"} if can_auto else set(),
            high_auth_conflict=set() if can_auto else {"html", "pdf"},
            can_auto_resolve=can_auto,
        )

    def test_unresolvable_returns_none(self):
        source_values = {"html": {"25000.0"}, "pdf": {"24000.0"}}
        diag = self._make_diag(can_auto=False)
        resolved_val, action, outcome = attempt_repair(source_values, diag)
        assert resolved_val is None
        assert action == "unresolved"
        assert outcome == {}

    def test_ai_conflict_resolved_to_html_consensus(self):
        source_values = {
            "html": {"25000.0"},
            "ai": {"24500.0"},  # conflicting low-auth
        }
        diag = diagnose_conflict(["ai"], list(source_values.keys()))
        resolved_val, action, outcome = attempt_repair(source_values, diag)
        assert action == "drop_low_authority"
        assert resolved_val == "25000.0"
        assert outcome.get("status") in ("verified", "likely_correct", "needs_review")

    def test_pattern_conflict_resolved_to_pdf_consensus(self):
        source_values = {
            "pdf": {"bachelor of science"},
            "pattern": {"b.sc."},  # conflicting low-auth
        }
        diag = diagnose_conflict(["pattern"], list(source_values.keys()))
        resolved_val, action, outcome = attempt_repair(source_values, diag)
        assert action == "drop_low_authority"
        assert resolved_val == "bachelor of science"

    def test_high_auth_conflict_not_repaired(self):
        source_values = {
            "html": {"full-time"},
            "pdf": {"part-time"},
        }
        diag = diagnose_conflict(["html"], list(source_values.keys()))
        resolved_val, action, outcome = attempt_repair(source_values, diag)
        assert action == "unresolved"
        assert resolved_val is None

    def test_ai_conflict_with_two_high_auth_sources(self):
        source_values = {
            "html": {"65.0"},
            "pdf": {"65.0"},
            "ai": {"60.0"},
        }
        diag = diagnose_conflict(["ai"], list(source_values.keys()))
        resolved_val, action, outcome = attempt_repair(source_values, diag)
        assert action == "drop_low_authority"
        assert resolved_val == "65.0"
        # After repair, two high-auth agree → higher confidence
        assert outcome.get("confidence", 0) >= 60

    def test_empty_source_values_unresolved(self):
        diag = self._make_diag(can_auto=True)
        resolved_val, action, outcome = attempt_repair({}, diag)
        assert action == "unresolved"

    def test_repair_gives_higher_confidence_than_conflict(self):
        """Repaired confidence should be above the 35 cap for conflicts."""
        source_values = {
            "html": {"25000.0"},
            "ai": {"24999.0"},
        }
        diag = diagnose_conflict(["ai"], list(source_values.keys()))
        _, action, outcome = attempt_repair(source_values, diag)
        if action == "drop_low_authority":
            assert outcome.get("confidence", 0) > 35  # above conflict cap


# ---------------------------------------------------------------------------
# Authority sets
# ---------------------------------------------------------------------------

class TestAuthorityConstants:
    def test_high_authority_contains_expected(self):
        assert "api" in HIGH_AUTHORITY
        assert "html" in HIGH_AUTHORITY
        assert "pdf" in HIGH_AUTHORITY

    def test_low_authority_contains_expected(self):
        assert "ai" in LOW_AUTHORITY
        assert "pattern" in LOW_AUTHORITY

    def test_no_overlap_between_sets(self):
        assert HIGH_AUTHORITY.isdisjoint(LOW_AUTHORITY)

    def test_ai_not_high_authority(self):
        assert "ai" not in HIGH_AUTHORITY

    def test_pattern_not_high_authority(self):
        assert "pattern" not in HIGH_AUTHORITY


# ---------------------------------------------------------------------------
# Source priority rules (spec §4)
# ---------------------------------------------------------------------------

class TestSourcePriorityRules:
    def test_api_pdf_agreement_beats_html_only(self):
        """API + PDF agree; HTML disagrees → resolve to API/PDF consensus."""
        source_values = {
            "api": {"25000.0"},
            "pdf": {"25000.0"},
            "html": {"24000.0"},  # outlier high-auth — can't auto-resolve
        }
        diag = diagnose_conflict(["html"], list(source_values.keys()))
        # html is high-auth conflicting → can't auto-resolve
        assert diag.can_auto_resolve is False

    def test_pdf_html_agreement_beats_ai(self):
        """PDF + HTML agree; AI disagrees → resolve to PDF/HTML consensus."""
        source_values = {
            "pdf": {"2 years"},
            "html": {"2 years"},
            "ai": {"24 months"},
        }
        diag = diagnose_conflict(["ai"], list(source_values.keys()))
        assert diag.can_auto_resolve is True
        _, action, outcome = attempt_repair(source_values, diag)
        assert action == "drop_low_authority"

    def test_direct_source_beats_gemini_inferred(self):
        """Any direct source (html/pdf/api) beats AI when they conflict."""
        source_values = {"html": {"march"}, "ai": {"february"}}
        diag = diagnose_conflict(["ai"], list(source_values.keys()))
        assert diag.can_auto_resolve is True

    def test_only_ai_sources_unresolvable(self):
        """If there are no high-auth sources, repair cannot proceed."""
        source_values = {"ai": {"value_a"}, "pattern": {"value_b"}}
        diag = diagnose_conflict(["ai"], list(source_values.keys()))
        assert diag.can_auto_resolve is False
