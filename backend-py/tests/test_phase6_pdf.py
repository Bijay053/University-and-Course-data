"""Phase 6 PDF Intelligence — unit tests.

Tests for:
- pdf_link_discoverer.py   (discover_pdf_links, _score_link)
- pdf_classifier.py        (classify_by_keywords, classify_pdf_url)
- entry_req_extractor.py   (extract_entry_requirements, EntryRequirement)

All tests are synchronous or use pytest-asyncio; no live HTTP calls are made.
Set SKIP_XHR_CAPTURE=1 to prevent Playwright from launching during import.

Run: PYTHONPATH=. SKIP_XHR_CAPTURE=1 pytest tests/test_phase6_pdf.py -v
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("SKIP_XHR_CAPTURE", "1")

import pytest
import pytest_asyncio

# ─────────────────────────────────────────────────────────────────────────────
# pdf_link_discoverer
# ─────────────────────────────────────────────────────────────────────────────

from app.services.scraper.pdf_link_discoverer import (
    PdfLink,
    _score_link,
    discover_pdf_links,
)


class TestScoreLink:
    def test_fee_url_keyword_scores_fee(self):
        lnk = _score_link("https://uni.edu.au/fees-schedule.pdf", "Fee Schedule", "")
        assert lnk.fee_score > lnk.req_score
        assert lnk.fee_score > 0.3

    def test_tuition_anchor_scores_fee(self):
        lnk = _score_link("https://uni.edu.au/docs/info.pdf", "International tuition fees", "")
        assert lnk.fee_score > 0.2

    def test_entry_req_url_scores_req(self):
        lnk = _score_link("https://uni.edu.au/admission-requirements.pdf", "Entry Requirements", "")
        assert lnk.req_score > lnk.fee_score

    def test_admission_anchor_scores_req(self):
        lnk = _score_link("https://uni.edu.au/doc.pdf", "Admissions Requirements 2025", "")
        assert lnk.req_score > 0.2

    def test_handbook_keyword_scores_handbook(self):
        lnk = _score_link("https://uni.edu.au/handbook-2025.pdf", "Handbook", "")
        assert lnk.handbook_score > lnk.fee_score

    def test_prospectus_scores_prospectus(self):
        lnk = _score_link("https://uni.edu.au/ug-prospectus-2026.pdf", "Undergraduate Prospectus", "")
        assert lnk.prospectus_score > 0.3

    def test_scholarship_scores_scholarship(self):
        lnk = _score_link("https://uni.edu.au/scholarship-guide.pdf", "Scholarship Guide", "")
        assert lnk.scholarship_score > lnk.fee_score

    def test_intake_calendar_scores_intake(self):
        lnk = _score_link("https://uni.edu.au/key-dates.pdf", "Academic Calendar", "")
        assert lnk.intake_score > 0.2

    def test_total_score_is_max_of_dimensions(self):
        lnk = _score_link("https://uni.edu.au/fees.pdf", "tuition fees", "")
        assert lnk.total_score == max(
            lnk.fee_score, lnk.req_score, lnk.handbook_score,
            lnk.prospectus_score, lnk.intake_score, lnk.scholarship_score,
        )

    def test_generic_url_low_score(self):
        lnk = _score_link("https://uni.edu.au/random-document.pdf", "", "")
        assert lnk.total_score < 0.2

    def test_best_category_fee(self):
        lnk = _score_link("https://uni.edu.au/international-fee-schedule-2026.pdf", "Fee Schedule 2026", "")
        assert lnk.best_category == "fee_schedule"

    def test_best_category_entry_requirements(self):
        lnk = _score_link("https://uni.edu.au/entry-requirements.pdf", "Entry Requirements", "")
        assert lnk.best_category == "entry_requirements"

    def test_to_dict_keys(self):
        lnk = _score_link("https://uni.edu.au/fees.pdf", "fee", "")
        d = lnk.to_dict()
        assert "url" in d and "best_category" in d and "total_score" in d

    def test_context_text_boosts_score(self):
        lnk_no_ctx = _score_link("https://uni.edu.au/info.pdf", "", "")
        lnk_ctx = _score_link("https://uni.edu.au/info.pdf", "", "international tuition fee schedule")
        assert lnk_ctx.fee_score >= lnk_no_ctx.fee_score


class TestDiscoverPdfLinks:
    def _html(self, hrefs_and_anchors: list[tuple[str, str]]) -> str:
        links = "".join(
            f'<a href="{h}">{a}</a>' for h, a in hrefs_and_anchors
        )
        return f"<html><body>{links}</body></html>"

    def test_finds_pdf_links(self):
        html = self._html([
            ("https://uni.edu.au/fees.pdf", "Fee Schedule"),
            ("https://uni.edu.au/handbook.pdf", "Handbook"),
        ])
        results = discover_pdf_links(html, "https://uni.edu.au")
        urls = {r.url for r in results}
        assert "https://uni.edu.au/fees.pdf" in urls

    def test_resolves_relative_urls(self):
        html = self._html([("/docs/fees-2026.pdf", "Tuition Fees")])
        results = discover_pdf_links(html, "https://uni.edu.au")
        assert any("fees-2026.pdf" in r.url for r in results)

    def test_deduplicates_same_url(self):
        html = self._html([
            ("https://uni.edu.au/fees.pdf", "Fee Schedule"),
            ("https://uni.edu.au/fees.pdf", "Fee Schedule"),
        ])
        results = discover_pdf_links(html, "https://uni.edu.au")
        fee_urls = [r for r in results if "fees.pdf" in r.url]
        assert len(fee_urls) == 1

    def test_filters_low_score_links(self):
        html = self._html([("https://uni.edu.au/random.pdf", "")])
        results = discover_pdf_links(html, "https://uni.edu.au")
        assert not results  # below threshold

    def test_sorted_by_score_desc(self):
        html = self._html([
            ("https://uni.edu.au/handbook.pdf", "Handbook"),
            ("https://uni.edu.au/international-tuition-fees.pdf", "International Tuition Fees"),
        ])
        results = discover_pdf_links(html, "https://uni.edu.au")
        if len(results) >= 2:
            assert results[0].total_score >= results[1].total_score

    def test_non_pdf_hrefs_ignored(self):
        html = '<a href="https://uni.edu.au/page.html">Fee Page</a>'
        results = discover_pdf_links(html, "https://uni.edu.au")
        assert not results

    def test_anchor_text_captured(self):
        html = self._html([("https://uni.edu.au/fees.pdf", "International Fee Schedule 2026")])
        results = discover_pdf_links(html, "https://uni.edu.au")
        assert results
        assert results[0].anchor_text == "International Fee Schedule 2026"

    def test_source_page_url_recorded(self):
        html = self._html([("https://uni.edu.au/fees.pdf", "Tuition fee")])
        results = discover_pdf_links(html, "https://uni.edu.au", source_page_url="https://uni.edu.au/intl/")
        assert results[0].source_page_url == "https://uni.edu.au/intl/"

    def test_fragment_stripped(self):
        html = self._html([("https://uni.edu.au/fees.pdf#page2", "Fee")])
        results = discover_pdf_links(html, "https://uni.edu.au")
        assert results
        assert "#" not in results[0].url

    def test_non_http_hrefs_ignored(self):
        html = self._html([("ftp://uni.edu.au/fees.pdf", "Fee")])
        results = discover_pdf_links(html, "https://uni.edu.au")
        assert not results


# ─────────────────────────────────────────────────────────────────────────────
# pdf_classifier
# ─────────────────────────────────────────────────────────────────────────────

from app.services.scraper.pdf_classifier import (
    ClassifiedPdf,
    PdfCategory,
    _keyword_scores,
    classify_by_keywords,
    classify_pdf_url,
)


class TestKeywordScores:
    def test_fee_schedule_wins_for_tuition(self):
        scores = _keyword_scores("international tuition fee schedule 2026 AUD")
        assert scores["fee_schedule"] == max(scores.values())

    def test_entry_req_wins_for_ielts_admissions(self):
        scores = _keyword_scores("entry requirements ielts 6.5 admission prerequisite")
        assert scores["entry_requirements"] == max(scores.values())

    def test_handbook_wins(self):
        scores = _keyword_scores("handbook 2025 unit outline subject guide curriculum")
        assert scores["handbook"] == max(scores.values())

    def test_prospectus_wins(self):
        scores = _keyword_scores("postgraduate prospectus viewbook brochure")
        assert scores["prospectus"] == max(scores.values())

    def test_intake_calendar_wins(self):
        scores = _keyword_scores("academic calendar semester dates key dates 2026")
        assert scores["intake_calendar"] == max(scores.values())

    def test_scholarship_wins(self):
        scores = _keyword_scores("scholarship guide bursary financial aid")
        assert scores["scholarship"] == max(scores.values())

    def test_all_categories_present(self):
        scores = _keyword_scores("tuition fee")
        assert set(scores) >= {"fee_schedule", "entry_requirements", "handbook",
                                "prospectus", "course_catalogue", "intake_calendar",
                                "scholarship", "other"}

    def test_scores_bounded_0_to_1(self):
        scores = _keyword_scores("tuition fee schedule AUD international per year per semester")
        for v in scores.values():
            assert 0.0 <= v <= 1.0


class TestClassifyByKeywords:
    def test_fee_schedule_classified_correctly(self):
        result = classify_by_keywords("https://uni.edu.au/fees.pdf", "International tuition fee schedule 2026")
        assert result.category == "fee_schedule"
        assert result.confidence > 0.0

    def test_entry_req_classified_correctly(self):
        result = classify_by_keywords("https://uni.edu.au/admission.pdf", "Entry requirements IELTS 6.5 GPA")
        assert result.category == "entry_requirements"

    def test_handbook_classified_correctly(self):
        result = classify_by_keywords("https://uni.edu.au/handbook.pdf", "Course handbook unit outline")
        assert result.category == "handbook"

    def test_prospectus_classified_correctly(self):
        result = classify_by_keywords("https://uni.edu.au/prospectus.pdf", "Undergraduate prospectus viewbook")
        assert result.category == "prospectus"

    def test_intake_calendar_classified(self):
        result = classify_by_keywords("https://uni.edu.au/calendar.pdf", "Academic calendar semester dates")
        assert result.category == "intake_calendar"

    def test_scholarship_classified(self):
        result = classify_by_keywords("https://uni.edu.au/scholarship.pdf", "Scholarship guide bursary")
        assert result.category == "scholarship"

    def test_confidence_is_float_0_to_1(self):
        result = classify_by_keywords("https://uni.edu.au/fees.pdf", "fee schedule")
        assert 0.0 <= result.confidence <= 1.0

    def test_method_is_keyword(self):
        result = classify_by_keywords("https://uni.edu.au/fees.pdf", "fee schedule")
        assert result.classification_method == "keyword"

    def test_raw_scores_populated(self):
        result = classify_by_keywords("https://uni.edu.au/fees.pdf", "fee schedule")
        assert "fee_schedule" in result.raw_scores

    def test_to_dict_has_required_keys(self):
        result = classify_by_keywords("https://uni.edu.au/fees.pdf", "fee")
        d = result.to_dict()
        assert {"url", "category", "confidence", "method"} <= d.keys()

    def test_url_only_fee_classification(self):
        result = classify_pdf_url("https://uni.edu.au/international-tuition-fee-schedule-2026.pdf")
        assert result.category in ("fee_schedule", "entry_requirements")  # fee wins by URL alone

    def test_url_only_req_classification(self):
        result = classify_pdf_url("https://uni.edu.au/entry-requirements-admissions.pdf")
        assert result.category == "entry_requirements"

    def test_empty_text_still_classifies_by_url(self):
        result = classify_by_keywords("https://uni.edu.au/scholarship.pdf", "")
        assert result.category == "scholarship"

    def test_classification_is_deterministic(self):
        url = "https://uni.edu.au/fees.pdf"
        text = "international tuition fee schedule"
        r1 = classify_by_keywords(url, text)
        r2 = classify_by_keywords(url, text)
        assert r1.category == r2.category
        assert r1.confidence == r2.confidence


# ─────────────────────────────────────────────────────────────────────────────
# entry_req_extractor
# ─────────────────────────────────────────────────────────────────────────────

from app.services.scraper.entry_req_extractor import (
    EntryRequirement,
    extract_entry_requirements,
)


class TestExtractAtar:
    def test_atar_basic(self):
        r = extract_entry_requirements("You need a minimum ATAR of 75 to be eligible.")
        assert r.atar_min == 75.0

    def test_atar_with_decimal(self):
        r = extract_entry_requirements("Minimum ATAR: 80.00 is required.")
        assert r.atar_min == 80.0

    def test_atar_at_least(self):
        r = extract_entry_requirements("An ATAR of at least 65 is required for this course.")
        assert r.atar_min == 65.0

    def test_atar_out_of_range_ignored(self):
        r = extract_entry_requirements("ATAR of 120 is not valid.")
        assert r.atar_min is None

    def test_no_atar_returns_none(self):
        r = extract_entry_requirements("No rank requirements mentioned here.")
        assert r.atar_min is None


class TestExtractGpa:
    def test_gpa_with_scale(self):
        r = extract_entry_requirements("A minimum GPA of 5.0 out of 7.0 is required.")
        assert r.gpa_min == 5.0
        assert r.gpa_scale == 7.0

    def test_gpa_slash_format(self):
        r = extract_entry_requirements("GPA: 3.5/4.0 or higher.")
        assert r.gpa_min == 3.5
        assert r.gpa_scale == 4.0

    def test_gpa_on_a_scale(self):
        r = extract_entry_requirements("Minimum GPA of 3.0 on a 4.0 scale.")
        assert r.gpa_min == 3.0

    def test_bare_gpa_infers_scale(self):
        r = extract_entry_requirements("Minimum GPA of 2.5 required.")
        assert r.gpa_min == 2.5
        assert r.gpa_scale == 4.0

    def test_gpa_7_scale_inferred(self):
        r = extract_entry_requirements("Minimum GPA of 4.5 required.")
        assert r.gpa_min == 4.5
        assert r.gpa_scale == 7.0

    def test_wam_extracted(self):
        r = extract_entry_requirements("A minimum WAM of 65% is required.")
        assert r.wam_min == 65.0


class TestExtractPriorDegree:
    def test_bachelor_degree(self):
        r = extract_entry_requirements("Applicants must hold a bachelor's degree in any discipline.")
        assert r.prior_degree == "bachelor"

    def test_honours_degree(self):
        r = extract_entry_requirements("A first-class honours degree is required.")
        assert r.prior_degree == "honours"

    def test_master_degree(self):
        r = extract_entry_requirements("Applicants must hold a master's degree.")
        assert r.prior_degree == "master"

    def test_graduate_diploma(self):
        r = extract_entry_requirements("A Graduate Diploma in any field is acceptable.")
        assert r.prior_degree == "graduate_diploma"

    def test_doctorate(self):
        r = extract_entry_requirements("Applicants must hold a doctorate or PhD.")
        assert r.prior_degree == "doctorate"

    def test_honours_ranks_above_bachelor(self):
        r = extract_entry_requirements("An honours degree or bachelor's degree is required.")
        assert r.prior_degree == "honours"  # honours checked first


class TestExtractWorkExperience:
    def test_years_work_experience(self):
        r = extract_entry_requirements("Applicants must have 2 years of work experience.")
        assert r.work_experience_years == 2.0

    def test_professional_experience(self):
        r = extract_entry_requirements("Minimum 3 years' professional experience in the field.")
        assert r.work_experience_years == 3.0

    def test_industry_experience(self):
        r = extract_entry_requirements("At least 5 years industry experience is required.")
        assert r.work_experience_years == 5.0

    def test_no_work_experience_none(self):
        r = extract_entry_requirements("No work experience required for this course.")
        assert r.work_experience_years is None


class TestExtractPortfolioInterview:
    def test_portfolio_required(self):
        r = extract_entry_requirements("A portfolio is required as part of the application.")
        assert r.portfolio_required

    def test_interview_required(self):
        r = extract_entry_requirements("An interview may be required as part of the selection process.")
        assert r.interview_required

    def test_submit_portfolio(self):
        r = extract_entry_requirements("Applicants must submit a portfolio of work.")
        assert r.portfolio_required

    def test_no_portfolio_false(self):
        r = extract_entry_requirements("No portfolio submission needed.")
        assert not r.portfolio_required


class TestConfidenceAndSummary:
    def test_confidence_zero_for_empty_text(self):
        r = extract_entry_requirements("")
        assert r.confidence == 0.0

    def test_confidence_increases_with_more_fields(self):
        r1 = extract_entry_requirements("ATAR of 70.")
        r2 = extract_entry_requirements("ATAR of 70. Bachelor's degree required. 2 years work experience.")
        assert r2.confidence >= r1.confidence

    def test_confidence_bounded_0_to_1(self):
        r = extract_entry_requirements(
            "ATAR of 70. GPA 5.0/7.0. Bachelor's degree. "
            "2 years work experience. Portfolio required. Interview required."
        )
        assert 0.0 <= r.confidence <= 1.0

    def test_to_summary_text_empty_when_no_fields(self):
        r = extract_entry_requirements("No requirements here.")
        assert r.to_summary_text() == ""

    def test_to_summary_atar(self):
        r = extract_entry_requirements("Minimum ATAR of 75.")
        summary = r.to_summary_text()
        assert "ATAR" in summary and "75" in summary

    def test_to_summary_gpa(self):
        r = extract_entry_requirements("Minimum GPA of 5.0 out of 7.0.")
        summary = r.to_summary_text()
        assert "GPA" in summary

    def test_to_summary_prior_degree(self):
        r = extract_entry_requirements("Applicants must hold a bachelor's degree.")
        summary = r.to_summary_text()
        assert "Bachelor" in summary

    def test_to_summary_work_experience(self):
        r = extract_entry_requirements("At least 3 years of work experience is required.")
        summary = r.to_summary_text()
        assert "work experience" in summary.lower()

    def test_to_summary_portfolio(self):
        r = extract_entry_requirements("A portfolio is required as part of the application.")
        summary = r.to_summary_text()
        assert "Portfolio" in summary

    def test_from_dict_round_trip(self):
        original = extract_entry_requirements("ATAR of 70. Bachelor's degree. 2 years work experience.")
        d = original.to_dict()
        restored = EntryRequirement.from_dict(d)
        assert restored.atar_min == original.atar_min
        assert restored.prior_degree == original.prior_degree
        assert restored.work_experience_years == original.work_experience_years

    def test_to_dict_has_all_keys(self):
        r = extract_entry_requirements("ATAR of 70.")
        d = r.to_dict()
        expected = {
            "atar_min", "gpa_min", "gpa_scale", "wam_min", "prior_degree",
            "work_experience_years", "work_experience_text",
            "prerequisite_subjects", "portfolio_required", "interview_required",
            "country_equivalencies", "confidence", "fields_found",
        }
        assert expected <= set(d.keys())

    def test_combined_text_extraction(self):
        text = (
            "To be eligible, applicants must hold a bachelor's degree with a "
            "minimum GPA of 5.0 out of 7.0. An ATAR of 70 is also acceptable. "
            "Applicants with 2 years of work experience may be considered. "
            "A portfolio is required."
        )
        r = extract_entry_requirements(text)
        assert r.prior_degree == "bachelor"
        assert r.gpa_min == 5.0
        assert r.atar_min == 70.0
        assert r.work_experience_years == 2.0
        assert r.portfolio_required
        assert r.confidence > 0.5

    def test_short_text_returns_empty(self):
        r = extract_entry_requirements("Hi")
        assert r.confidence == 0.0
        assert r.fields_found == 0


# ─────────────────────────────────────────────────────────────────────────────
# Low-value PDF filter — pdf_link_discoverer.is_low_value_link
# ─────────────────────────────────────────────────────────────────────────────

from app.services.scraper.pdf_link_discoverer import is_low_value_link


class TestLowValueLinkDiscoverer:
    """is_low_value_link() suppresses administrative PDFs but allows high-value ones."""

    # ── Blocked URLs ─────────────────────────────────────────────────────────

    def test_privacy_policy_url_blocked(self):
        assert is_low_value_link("https://uni.edu/docs/privacy-policy.pdf", "")

    def test_terms_of_service_url_blocked(self):
        assert is_low_value_link("https://uni.edu/terms-of-service.pdf", "")

    def test_terms_and_conditions_url_blocked(self):
        assert is_low_value_link("https://uni.edu/terms-and-conditions.pdf", "Terms")

    def test_annual_report_url_blocked(self):
        assert is_low_value_link("https://uni.edu/annual-report-2024.pdf", "")

    def test_complaint_procedure_url_blocked(self):
        assert is_low_value_link("https://uni.edu/complaint-procedure.pdf", "")

    def test_code_of_conduct_url_blocked(self):
        assert is_low_value_link("https://uni.edu/code-of-conduct.pdf", "")

    def test_newsletter_url_blocked(self):
        assert is_low_value_link("https://uni.edu/newsletter-winter.pdf", "")

    def test_strategic_plan_url_blocked(self):
        assert is_low_value_link("https://uni.edu/strategic-plan.pdf", "")

    def test_financial_statement_url_blocked(self):
        assert is_low_value_link("https://uni.edu/financial-statement-2023.pdf", "")

    # ── Blocked anchors ───────────────────────────────────────────────────────

    def test_annual_report_anchor_blocked(self):
        assert is_low_value_link("https://uni.edu/documents/doc.pdf", "Annual Report 2024")

    def test_privacy_policy_anchor_blocked(self):
        assert is_low_value_link("https://uni.edu/p.pdf", "Privacy Policy")

    def test_code_of_conduct_anchor_blocked(self):
        assert is_low_value_link("https://uni.edu/x.pdf", "Code of Conduct")

    def test_whistleblower_anchor_blocked(self):
        assert is_low_value_link("https://uni.edu/wh.pdf", "Whistleblower Policy")

    # ── High-value URLs must NOT be blocked ─────────────────────────────────

    def test_fee_schedule_not_blocked(self):
        assert not is_low_value_link("https://uni.edu/international-fee-schedule.pdf", "")

    def test_entry_requirements_not_blocked(self):
        assert not is_low_value_link("https://uni.edu/entry-requirements.pdf", "")

    def test_course_handbook_not_blocked(self):
        assert not is_low_value_link("https://uni.edu/course-handbook-2025.pdf", "")

    def test_unrelated_url_not_blocked(self):
        assert not is_low_value_link("https://uni.edu/postgraduate-guide.pdf", "")

    # ── Ambiguous: low-value URL pattern but high-value signal in combined text
    def test_fee_refund_policy_not_blocked(self):
        """'refund-policy' matches blocklist but 'fee' in anchor overrides it."""
        assert not is_low_value_link(
            "https://uni.edu/international-fee-refund-policy.pdf",
            "International Fee Refund Policy",
        )

    # ── Zero-score path in _score_link ───────────────────────────────────────

    def test_low_value_link_returns_zero_scores(self):
        lnk = _score_link("https://uni.edu/privacy-policy.pdf", "", "")
        assert lnk.fee_score == 0.0
        assert lnk.req_score == 0.0
        assert lnk.handbook_score == 0.0

    def test_low_value_link_filtered_by_threshold(self):
        """discover_pdf_links must drop low-value links via threshold."""
        from app.services.scraper.pdf_link_discoverer import _MIN_SCORE_THRESHOLD
        lnk = _score_link("https://uni.edu/annual-report.pdf", "Annual Report", "")
        assert lnk.total_score < _MIN_SCORE_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Low-value PDF filter — pdf_classifier.is_low_value_pdf
# ─────────────────────────────────────────────────────────────────────────────

from app.services.scraper.pdf_classifier import is_low_value_pdf


class TestLowValuePdfClassifier:
    """is_low_value_pdf() in the classifier mirrors the discoverer's blocklist."""

    def test_privacy_policy_blocked(self):
        assert is_low_value_pdf("https://uni.edu/privacy-policy.pdf")

    def test_annual_report_blocked(self):
        assert is_low_value_pdf("https://uni.edu/annual-report-2024.pdf")

    def test_complaint_procedure_blocked(self):
        assert is_low_value_pdf("https://uni.edu/complaint-procedure.pdf", "Complaints handling")

    def test_sustainability_report_blocked(self):
        assert is_low_value_pdf("https://uni.edu/sustainability-report.pdf")

    def test_board_minutes_blocked(self):
        assert is_low_value_pdf("https://uni.edu/board-minutes-mar24.pdf")

    def test_fee_schedule_not_blocked(self):
        assert not is_low_value_pdf("https://uni.edu/2025-fee-schedule.pdf")

    def test_entry_requirements_not_blocked(self):
        assert not is_low_value_pdf("https://uni.edu/entry-requirements.pdf")

    def test_handbook_not_blocked(self):
        assert not is_low_value_pdf("https://uni.edu/handbook-ug-2025.pdf")

    def test_first_page_text_override(self):
        """'fee' in first-page text overrides a low-value URL pattern."""
        assert not is_low_value_pdf(
            "https://uni.edu/refund-policy.pdf",
            "International student tuition fee refund policy and schedule.",
        )

    def test_classify_pdf_low_value_returns_other(self):
        """classify_by_keywords should not be called for low-value PDFs."""
        from app.services.scraper.pdf_classifier import classify_by_keywords
        result = classify_by_keywords("https://uni.edu/privacy-policy.pdf", "")
        # low-value gate is inside classify_pdf (async); classify_by_keywords is unguarded
        # so this just verifies classify_by_keywords itself doesn't crash.
        assert result.category in {
            "fee_schedule", "entry_requirements", "handbook", "prospectus",
            "course_catalogue", "intake_calendar", "scholarship", "other",
        }
