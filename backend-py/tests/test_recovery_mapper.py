"""Unit tests for the recovery mapper.

Covers:
  - _normalise_level: various degree_level strings → internal buckets
  - _url_level_bucket: URL path segment → level bucket or None
  - _snippet_level_bucket: predominant keyword bucket in text snippet
  - _is_disqualified: snippet vs URL disqualification rules
  - _score_result: scoring criteria (snippet match, URL match, course-name words, confidence)
  - map_results_to_course: best-result selection, return_rejects, None value skipping,
    disqualified results tracked in rejects, multiple fields, no-result edge case
"""
from __future__ import annotations

import pytest

from app.services.scraper.recovery.mapper import (
    _is_disqualified,
    _normalise_level,
    _score_result,
    _snippet_level_bucket,
    _url_level_bucket,
    map_results_to_course,
)


# ---------------------------------------------------------------------------
# _normalise_level
# ---------------------------------------------------------------------------

class TestNormaliseLevel:
    def test_bachelors(self):
        assert _normalise_level("Bachelor's Degree") == "undergraduate"

    def test_honours(self):
        assert _normalise_level("Honours") == "undergraduate"

    def test_diploma(self):
        assert _normalise_level("Diploma") == "undergraduate"

    def test_associate(self):
        assert _normalise_level("Associate Degree") == "undergraduate"

    def test_masters(self):
        assert _normalise_level("Master of Science") == "postgraduate"

    def test_mba(self):
        assert _normalise_level("MBA") == "postgraduate"

    def test_graduate_certificate(self):
        # "certificate" keyword is in the undergraduate bucket and matches first
        # in iteration order, so "Graduate Certificate" → "undergraduate"
        assert _normalise_level("Graduate Certificate") == "undergraduate"

    def test_phd_returns_postgraduate(self):
        # "phd" appears in the postgraduate bucket before the research bucket
        assert _normalise_level("PhD") == "postgraduate"

    def test_doctorate_returns_postgraduate(self):
        # "doctorate" appears in the postgraduate bucket before the research bucket
        assert _normalise_level("Doctorate") == "postgraduate"

    def test_none_returns_unknown(self):
        assert _normalise_level(None) == "unknown"

    def test_empty_returns_unknown(self):
        assert _normalise_level("") == "unknown"

    def test_unrecognised_returns_unknown(self):
        assert _normalise_level("Quelque chose d'inconnu") == "unknown"

    def test_case_insensitive(self):
        assert _normalise_level("BACHELOR") == "undergraduate"


# ---------------------------------------------------------------------------
# _url_level_bucket
# ---------------------------------------------------------------------------

class TestUrlLevelBucket:
    def test_undergraduate_in_path(self):
        assert _url_level_bucket("https://uni.edu.au/undergraduate/fees") == "undergraduate"

    def test_pg_abbreviation(self):
        assert _url_level_bucket("https://uni.edu.au/pg/courses/info") == "postgraduate"

    def test_research_in_path(self):
        assert _url_level_bucket("https://uni.edu.au/research/phd-degrees") == "research"

    def test_online_in_path(self):
        assert _url_level_bucket("https://uni.edu.au/online/fees") == "online"

    def test_neutral_path_returns_none(self):
        assert _url_level_bucket("https://uni.edu.au/courses/fees") is None

    def test_empty_url_returns_none(self):
        assert _url_level_bucket("") is None


# ---------------------------------------------------------------------------
# _snippet_level_bucket
# ---------------------------------------------------------------------------

class TestSnippetLevelBucket:
    def test_bachelor_in_snippet(self):
        assert _snippet_level_bucket("Bachelor of Engineering entry requirements") == "undergraduate"

    def test_master_in_snippet(self):
        assert _snippet_level_bucket("Master's degree applicants must hold...") == "postgraduate"

    def test_phd_in_snippet(self):
        result = _snippet_level_bucket("PhD candidates research doctorate programs")
        assert result in ("research", "postgraduate")

    def test_no_keywords_returns_none(self):
        assert _snippet_level_bucket("Tuition fees are listed below") is None

    def test_none_snippet_returns_none(self):
        assert _snippet_level_bucket(None) is None

    def test_empty_snippet_returns_none(self):
        assert _snippet_level_bucket("") is None


# ---------------------------------------------------------------------------
# _is_disqualified
# ---------------------------------------------------------------------------

class TestIsDisqualified:
    def _result(self, snippet="", url=""):
        return {"snippet": snippet, "source_url": url}

    def test_snippet_signals_different_level_disqualifies(self):
        result = self._result(
            snippet="Postgraduate master's degree fee schedule",
            url="https://uni.edu.au/courses/fees",
        )
        disq, reason = _is_disqualified(result, "undergraduate")
        assert disq is True
        assert "postgraduate" in reason

    def test_url_signals_different_level_no_snippet_disqualifies(self):
        result = self._result(
            snippet="Fee schedule 2025",
            url="https://uni.edu.au/postgraduate/fees",
        )
        disq, reason = _is_disqualified(result, "undergraduate")
        assert disq is True
        assert "postgraduate" in reason

    def test_snippet_confirms_correct_level_not_disqualified(self):
        result = self._result(
            snippet="Bachelor of Science students pay",
            url="https://uni.edu.au/undergraduate/fees",
        )
        disq, _ = _is_disqualified(result, "undergraduate")
        assert disq is False

    def test_url_wrong_but_snippet_correct_not_disqualified(self):
        result = self._result(
            snippet="Bachelor students — tuition",
            url="https://uni.edu.au/postgraduate/fees",
        )
        disq, _ = _is_disqualified(result, "undergraduate")
        assert disq is False

    def test_unknown_target_never_disqualified(self):
        result = self._result(
            snippet="Postgraduate fees",
            url="https://uni.edu.au/postgraduate/fees",
        )
        disq, _ = _is_disqualified(result, "unknown")
        assert disq is False

    def test_no_signals_not_disqualified(self):
        result = self._result(snippet="Fee schedule", url="https://uni.edu.au/fees")
        disq, _ = _is_disqualified(result, "undergraduate")
        assert disq is False


# ---------------------------------------------------------------------------
# _score_result
# ---------------------------------------------------------------------------

class TestScoreResult:
    def _result(self, snippet="", url="", confidence=None):
        r = {"snippet": snippet, "source_url": url}
        if confidence is not None:
            r["confidence"] = confidence
        return r

    def test_snippet_match_adds_positive_score(self):
        r = self._result(snippet="Bachelor of Arts fees for the year")
        score, _ = _score_result(r, "undergraduate", "Bachelor of Arts")
        assert score > 0

    def test_snippet_mismatch_penalises(self):
        r = self._result(snippet="Postgraduate master's tuition")
        score, _ = _score_result(r, "undergraduate", "Bachelor of Arts")
        assert score < 0

    def test_url_match_adds_score(self):
        r = self._result(url="https://uni.edu.au/undergraduate/fees")
        score, _ = _score_result(r, "undergraduate", "")
        assert score > 0

    def test_high_confidence_adds_bonus(self):
        r = self._result(confidence=0.90)
        score_high, _ = _score_result(r, "unknown", "")
        r_low = self._result(confidence=0.30)
        score_low, _ = _score_result(r_low, "unknown", "")
        assert score_high > score_low

    def test_course_name_words_in_snippet_adds_score(self):
        r = self._result(snippet="Doctor of Philosophy students research doctorate programs")
        score_match, _ = _score_result(r, "research", "Doctor of Philosophy")
        score_nomatch, _ = _score_result(
            self._result(snippet="Doctor of Philosophy students research doctorate programs"),
            "research",
            "Bachelor of Engineering",
        )
        assert score_match >= score_nomatch

    def test_reason_string_populated(self):
        r = self._result(snippet="Bachelor of Science", url="https://uni.edu/ug/fees")
        _, reason = _score_result(r, "undergraduate", "Bachelor of Science")
        assert isinstance(reason, str) and len(reason) > 0


# ---------------------------------------------------------------------------
# map_results_to_course
# ---------------------------------------------------------------------------

class TestMapResultsToCourse:
    def _result(self, field, value, snippet="", url="", confidence=None):
        r = {"field": field, "value": value, "snippet": snippet, "source_url": url}
        if confidence is not None:
            r["confidence"] = confidence
        return r

    def test_empty_results_returns_empty_dict(self):
        out = map_results_to_course([], degree_level="Bachelor's", course_name="Test")
        assert out == {}

    def test_accepted_result_present_in_output(self):
        results = [self._result("international_fee", 32000, url="https://uni.edu.au/fees")]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert "international_fee" in out
        assert out["international_fee"]["value"] == 32000

    def test_mapping_reason_added_to_result(self):
        results = [self._result("international_fee", 32000, url="https://uni.edu.au/fees")]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert "mapping_reason" in out["international_fee"]

    def test_none_value_result_is_skipped(self):
        results = [self._result("international_fee", None)]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert "international_fee" not in out

    def test_missing_field_key_skipped(self):
        results = [{"value": 32000, "source_url": "https://uni.edu.au"}]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert out == {}

    def test_disqualified_result_not_in_accepted(self):
        results = [
            self._result(
                "international_fee",
                50000,
                snippet="Postgraduate master's fees",
                url="https://uni.edu.au/pg/fees",
            )
        ]
        out = map_results_to_course(
            results, degree_level="Bachelor's", course_name="Bachelor of Arts"
        )
        assert "international_fee" not in out

    def test_return_rejects_false_returns_dict(self):
        results = [self._result("international_fee", 32000)]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert isinstance(out, dict)

    def test_return_rejects_true_returns_tuple(self):
        results = [self._result("international_fee", 32000)]
        out = map_results_to_course(
            results, degree_level=None, course_name="Test", return_rejects=True
        )
        assert isinstance(out, tuple) and len(out) == 2
        accepted, rejected = out
        assert isinstance(accepted, dict)
        assert isinstance(rejected, dict)

    def test_disqualified_appears_in_rejects(self):
        results = [
            self._result(
                "ielts_overall",
                7.0,
                snippet="Postgraduate master's entry requirement",
                url="https://uni.edu.au/pg/english",
            )
        ]
        accepted, rejected = map_results_to_course(
            results,
            degree_level="Bachelor's",
            course_name="Bachelor of Arts",
            return_rejects=True,
        )
        assert "ielts_overall" not in accepted
        assert "ielts_overall" in rejected
        assert len(rejected["ielts_overall"]) == 1
        assert "reason" in rejected["ielts_overall"][0]

    def test_best_score_wins_for_same_field(self):
        low = self._result(
            "international_fee",
            20000,
            snippet="Postgraduate fees",
            url="https://uni.edu.au/fees",
            confidence=0.30,
        )
        high = self._result(
            "international_fee",
            32000,
            snippet="Bachelor tuition",
            url="https://uni.edu.au/undergraduate/fees",
            confidence=0.85,
        )
        out = map_results_to_course(
            [low, high], degree_level="Bachelor's", course_name="Bachelor of Arts"
        )
        assert out["international_fee"]["value"] == 32000

    def test_multiple_fields_all_accepted(self):
        results = [
            self._result("international_fee", 32000),
            self._result("ielts_overall", 6.5),
            self._result("course_location", "Sydney"),
        ]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert "international_fee" in out
        assert "ielts_overall" in out
        assert "course_location" in out

    def test_mapping_score_stored_on_result(self):
        results = [self._result("course_location", "Melbourne")]
        out = map_results_to_course(results, degree_level=None, course_name="Test")
        assert "mapping_score" in out["course_location"]
