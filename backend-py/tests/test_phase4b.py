"""Phase 4B — Autonomous API Discovery: comprehensive test suite.

Tests cover:
- XhrCapture dataclass
- api_classifier: all 6 API types + edge cases
- api_schema_analyzer: keyword matching, value inference, confidence
- pattern_store: lookup_api_mapping / promote_api_mapping (sync stubs)
- generic_search_api: _navigate_path, _apply_field_mapping, _item_to_link
- site_probe: _capture_xhr_stage integration contract (mocked Playwright)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

import pytest


# ── XhrCapture ────────────────────────────────────────────────────────────────

class TestXhrCapture:
    def _cls(self):
        from app.services.scraper.xhr_interceptor import XhrCapture
        return XhrCapture

    def test_is_json_content_type(self):
        cls = self._cls()
        cap = cls(url="https://x.com/api", content_type="application/json")
        assert cap.is_json()

    def test_is_json_inferred_from_body(self):
        cls = self._cls()
        cap = cls(url="https://x.com/api", content_type="text/plain", sample_body={"a": 1})
        assert cap.is_json()

    def test_not_json(self):
        cls = self._cls()
        cap = cls(url="https://x.com/api", content_type="text/html", sample_body=None)
        assert not cap.is_json()

    def test_default_fields(self):
        cls = self._cls()
        cap = cls(url="https://x.com/api")
        assert cap.method == "GET"
        assert cap.body_size == 0
        assert cap.request_headers == {}
        assert cap.sample_body is None

    def test_body_size_stored(self):
        cls = self._cls()
        cap = cls(url="u", body_size=12345)
        assert cap.body_size == 12345


# ── API Classifier ────────────────────────────────────────────────────────────

def _make_capture(url: str, body: Any):
    from app.services.scraper.xhr_interceptor import XhrCapture
    return XhrCapture(url=url, sample_body=body, content_type="application/json", body_size=len(str(body)))


_ALGOLIA_BODY = {"hits": [{"title": "Course A", "url": "/course-a"}], "nbHits": 1, "nbPages": 1}
_ES_BODY = {"hits": {"hits": [{"_source": {"name": "Course B"}}], "total": {"value": 1}}, "_shards": {}}
_SOLR_BODY = {"responseHeader": {"status": 0}, "response": {"docs": [{"title": "Course C"}], "numFound": 1}}
_SEARCHSTAX_BODY = {"responseHeader": {}, "response": {"docs": [{"h1": "Course D", "url": "/d"}]}}
_GRAPHQL_BODY = {"data": {"courses": [{"name": "Course E"}]}}
_REST_BODY = [{"title": "Course F", "link": "/f"}, {"title": "Course G", "link": "/g"}]


class TestApiClassifierAlgolia:
    def test_algolia_url_match(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://abc-dsn.algolia.net/1/indexes/courses", _ALGOLIA_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "algolia"

    def test_algolia_body_shape(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/search", _ALGOLIA_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "algolia"

    def test_algolia_results_path(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://abc.algolianet.com/search", _ALGOLIA_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.results_path == "hits"

    def test_algolia_confidence_high(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://abc.algolia.net/", _ALGOLIA_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.confidence >= 0.70


class TestApiClassifierElasticsearch:
    def test_elastic_url(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com:9200/courses/_search", _ES_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "elasticsearch"

    def test_elastic_body_shape(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/api/v1/search", _ES_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "elasticsearch"

    def test_elastic_results_path(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://search.example.com/_search", _ES_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.results_path == "hits.hits"


class TestApiClassifierSolr:
    def test_solr_url(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/solr/courses/select?wt=json", _SOLR_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type in ("solr", "searchstax")

    def test_solr_body_shape(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://api.example.com/courses", _SOLR_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type in ("solr",)

    def test_solr_results_path(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/solr/select?wt=json", _SOLR_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.results_path == "response.docs"


class TestApiClassifierSearchStax:
    def test_searchstax_url_wins(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture(
            "https://tenant-1234.searchstax.com/29847/myuni/emselect",
            _SEARCHSTAX_BODY,
        )
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "searchstax"

    def test_searchstax_beats_solr(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture(
            "https://tenant.searchstax.com/core/select?wt=json",
            _SOLR_BODY,
        )
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "searchstax"

    def test_searchstax_results_path(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://x.searchstax.com/emselect", _SEARCHSTAX_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.results_path == "response.docs"


class TestApiClassifierGraphQL:
    def test_graphql_url(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/graphql", _GRAPHQL_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "graphql"

    def test_graphql_results_path_resolves_list(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/api/graphql", _GRAPHQL_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.results_path == "data.courses"

    def test_graphql_no_url_but_data_shape(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/query", _GRAPHQL_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "graphql"


class TestApiClassifierRestJson:
    def test_top_level_array(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/courses.json", _REST_BODY)
        result = classify_capture(cap)
        assert result is not None
        assert result.api_type == "rest_json"

    def test_object_with_list(self):
        from app.services.scraper.api_classifier import classify_capture
        body = {"data": [{"name": "C1"}, {"name": "C2"}], "total": 2}
        cap = _make_capture("https://example.com/api/courses", body)
        result = classify_capture(cap)
        assert result is not None

    def test_no_match_empty_body(self):
        from app.services.scraper.api_classifier import classify_capture
        cap = _make_capture("https://example.com/api", None)
        assert classify_capture(cap) is None

    def test_no_match_html_body(self):
        from app.services.scraper.api_classifier import classify_capture
        from app.services.scraper.xhr_interceptor import XhrCapture
        cap = XhrCapture(url="https://x.com", sample_body=None, content_type="text/html")
        assert classify_capture(cap) is None


class TestClassifyCaptures:
    def test_returns_best_confidence(self):
        from app.services.scraper.api_classifier import classify_captures
        caps = [
            _make_capture("https://example.com/api", _REST_BODY),
            _make_capture("https://abc.algolia.net/search", _ALGOLIA_BODY),
        ]
        result = classify_captures(caps)
        assert result is not None
        assert result.api_type == "algolia"

    def test_empty_list_returns_none(self):
        from app.services.scraper.api_classifier import classify_captures
        assert classify_captures([]) is None

    def test_all_low_confidence_returns_none(self):
        from app.services.scraper.api_classifier import classify_captures
        from app.services.scraper.xhr_interceptor import XhrCapture
        caps = [XhrCapture(url="https://x.com", sample_body=None)]
        assert classify_captures(caps) is None


# ── API Schema Analyzer ───────────────────────────────────────────────────────

def _make_classified(api_type: str, body: Any, results_path: str = ""):
    from app.services.scraper.api_classifier import ClassifiedAPI
    return ClassifiedAPI(
        api_type=api_type,
        endpoint_url="https://example.com/api",
        confidence=0.8,
        sample_response=body,
        results_path=results_path,
    )


class TestSchemaAnalyzerKeywordMapping:
    def test_title_maps_to_course_name(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "Intro to CS", "url": "/courses/cs101"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "course_name" in result.field_mapping
        assert result.field_mapping["course_name"] == "title"

    def test_url_maps_to_url(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "Course", "url": "/courses/c1"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "url" in result.field_mapping

    def test_tuition_fee_maps_to_fee(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"course_name": "C", "url": "/c", "tuitionFee": 25000}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "fee_amount" in result.field_mapping

    def test_ielts_maps_to_english_score(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "ielts": 6.5}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "english_score" in result.field_mapping

    def test_degree_level_maps(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "degreeLevel": "Postgraduate"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "degree_level" in result.field_mapping

    def test_duration_maps(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "duration": "3 years"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "duration" in result.field_mapping

    def test_campus_maps_to_location(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "campus": "City"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "location" in result.field_mapping

    def test_description_maps(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "overview": "Long description " * 20}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "description" in result.field_mapping

    def test_study_mode_maps(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "studyMode": "Full-time"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert "study_mode" in result.field_mapping


class TestSchemaAnalyzerEdgeCases:
    def test_empty_body_returns_empty_mapping(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        api = _make_classified("algolia", {}, "hits")
        result = analyze_schema(api)
        assert result.field_mapping == {}
        assert result.overall_confidence == 0.0

    def test_no_items_in_results(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        api = _make_classified("algolia", {"hits": []}, "hits")
        result = analyze_schema(api)
        assert result.field_mapping == {}

    def test_overall_confidence_positive(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C", "url": "/c", "degreeLevel": "UG", "tuitionFee": 20000, "duration": "2 years"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        assert result.overall_confidence > 0.0

    def test_no_duplicate_api_path_used(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = {"hits": [{"title": "C"}]}
        api = _make_classified("algolia", body, "hits")
        result = analyze_schema(api)
        used_paths = list(result.field_mapping.values())
        assert len(used_paths) == len(set(used_paths))

    def test_elasticsearch_results_path(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        body = _ES_BODY
        api = _make_classified("elasticsearch", body, "hits.hits")
        result = analyze_schema(api)
        assert result.results_path == "hits.hits"

    def test_top_level_array_results(self):
        from app.services.scraper.api_schema_analyzer import analyze_schema
        api = _make_classified("rest_json", _REST_BODY, "")
        result = analyze_schema(api)
        # top-level array — items should be extracted
        assert isinstance(result.field_mapping, dict)

    def test_api_field_mapping_to_dict_roundtrip(self):
        from app.services.scraper.api_schema_analyzer import ApiFieldMapping
        m = ApiFieldMapping(
            field_mapping={"course_name": "title", "url": "permalink"},
            results_path="hits",
            api_type="algolia",
            overall_confidence=0.6,
        )
        d = m.to_dict()
        m2 = ApiFieldMapping.from_dict(d)
        assert m2.field_mapping == m.field_mapping
        assert m2.api_type == "algolia"
        assert m2.overall_confidence == 0.6


# ── Pattern store: lookup_api_mapping / promote_api_mapping ──────────────────

class TestPatternStoreApiMapping:
    def _lookup(self):
        from app.services.scraper.pattern_store import lookup_api_mapping
        return lookup_api_mapping

    def _promote(self):
        from app.services.scraper.pattern_store import promote_api_mapping
        return promote_api_mapping

    @pytest.mark.asyncio
    async def test_lookup_returns_none_on_miss(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))
        result = await self._lookup()("algolia", db)
        assert result is None

    @pytest.mark.asyncio
    async def test_lookup_returns_dict_on_hit(self):
        db = AsyncMock()
        stored = {"field_mapping": {"course_name": "title"}, "api_type": "algolia"}
        db.execute = AsyncMock(
            return_value=MagicMock(fetchone=MagicMock(return_value=(stored, 3, 0.82)))
        )
        result = await self._lookup()("algolia", db)
        assert result is not None
        assert result["field_mapping"]["course_name"] == "title"

    @pytest.mark.asyncio
    async def test_lookup_empty_api_type_returns_none(self):
        db = AsyncMock()
        result = await self._lookup()("", db)
        assert result is None
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_promote_below_threshold_skips(self):
        db = AsyncMock()
        fm = {"field_mapping": {"course_name": "title"}}
        fill_rates = {"course_name": 0.50}  # below 0.70
        result = await self._promote()("algolia", fm, fill_rates, db)
        assert result == 0
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_promote_above_threshold_executes(self):
        db = AsyncMock()
        fm = {"field_mapping": {"course_name": "title", "url": "permalink"}}
        fill_rates = {"course_name": 0.80, "url": 0.90}
        result = await self._promote()("algolia", fm, fill_rates, db)
        assert result == 1
        db.execute.assert_called_once()
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_promote_empty_fill_rates_skips(self):
        db = AsyncMock()
        fm = {"field_mapping": {"course_name": "title"}}
        result = await self._promote()("rest_json", fm, {}, db)
        assert result == 0  # avg_rate = 0.0 < 0.70

    @pytest.mark.asyncio
    async def test_promote_uses_api_prefix_in_platform_key(self):
        db = AsyncMock()
        fm = {"field_mapping": {"course_name": "name"}}
        fill_rates = {"course_name": 0.85}
        await self._promote()("elasticsearch", fm, fill_rates, db)
        # Verify the platform_type used "api:elasticsearch"
        call_args = db.execute.call_args
        assert call_args is not None
        params = call_args[0][1] if call_args[0] else call_args[1]
        if isinstance(params, dict):
            assert params.get("pt") == "api:elasticsearch"

    @pytest.mark.asyncio
    async def test_promote_handles_db_error_gracefully(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("DB error"))
        db.rollback = AsyncMock()
        fm = {"field_mapping": {"course_name": "title"}}
        fill_rates = {"course_name": 0.85}
        result = await self._promote()("algolia", fm, fill_rates, db)
        assert result == 0  # error handled gracefully


# ── generic_search_api: field-mapping helpers ─────────────────────────────────

class TestNavigatePath:
    def _fn(self):
        from app.services.scraper.generic_search_api import _navigate_path
        return _navigate_path

    def test_simple_key(self):
        assert self._fn()({"a": 1}, "a") == 1

    def test_nested_key(self):
        assert self._fn()({"a": {"b": 2}}, "a.b") == 2

    def test_missing_key_returns_none(self):
        assert self._fn()({"a": 1}, "b") is None

    def test_empty_path_returns_obj(self):
        assert self._fn()({"a": 1}, "") == {"a": 1}

    def test_list_index(self):
        assert self._fn()({"hits": [{"name": "C"}]}, "hits.0") == {"name": "C"}

    def test_deep_nested(self):
        obj = {"data": {"courses": {"items": [{"title": "X"}]}}}
        assert self._fn()(obj, "data.courses.items") == [{"title": "X"}]


class TestApplyFieldMapping:
    def _fn(self):
        from app.services.scraper.generic_search_api import _apply_field_mapping
        return _apply_field_mapping

    def test_simple_mapping(self):
        item = {"title": "Course A", "permalink": "/courses/a"}
        mapping = {"course_name": "title", "url": "permalink"}
        result = self._fn()(item, mapping)
        assert result == {"course_name": "Course A", "url": "/courses/a"}

    def test_missing_field_omitted(self):
        item = {"title": "Course A"}
        mapping = {"course_name": "title", "url": "permalink"}
        result = self._fn()(item, mapping)
        assert "url" not in result

    def test_nested_path_mapping(self):
        item = {"info": {"fee": 25000}, "title": "C"}
        mapping = {"fee_amount": "info.fee", "course_name": "title"}
        result = self._fn()(item, mapping)
        assert result["fee_amount"] == 25000

    def test_empty_string_omitted(self):
        item = {"title": ""}
        mapping = {"course_name": "title"}
        result = self._fn()(item, mapping)
        assert "course_name" not in result

    def test_none_value_omitted(self):
        item = {"fee": None}
        mapping = {"fee_amount": "fee"}
        result = self._fn()(item, mapping)
        assert "fee_amount" not in result


class TestItemToLink:
    def _fn(self):
        from app.services.scraper.generic_search_api import _item_to_link
        return _item_to_link

    def test_basic_link(self):
        item = {"title": "Intro CS", "link": "https://example.com/cs"}
        mapping = {"course_name": "title", "url": "link"}
        result = self._fn()(item, mapping)
        assert result is not None
        assert result["name"] == "Intro CS"
        assert result["url"] == "https://example.com/cs"

    def test_relative_url_made_absolute(self):
        item = {"title": "C", "url": "/courses/c"}
        mapping = {"course_name": "title", "url": "url"}
        result = self._fn()(item, mapping, base_url="https://example.com")
        assert result["url"] == "https://example.com/courses/c"

    def test_none_if_no_url_or_name(self):
        item = {"fee": 25000}
        mapping = {"fee_amount": "fee"}
        result = self._fn()(item, mapping)
        assert result is None

    def test_auto_extracted_added(self):
        item = {"title": "C", "url": "/c", "tuitionFee": 20000}
        mapping = {"course_name": "title", "url": "url", "fee_amount": "tuitionFee"}
        result = self._fn()(item, mapping)
        assert result is not None
        assert result.get("auto_extracted", {}).get("fee_amount") == 20000

    def test_url_and_name_not_in_auto_extracted(self):
        item = {"title": "C", "url": "/c"}
        mapping = {"course_name": "title", "url": "url"}
        result = self._fn()(item, mapping)
        assert "url" not in result.get("auto_extracted", {})
        assert "course_name" not in result.get("auto_extracted", {})


# ── _capture_xhr_stage integration: SiteProfile fields ───────────────────────

class TestCaptureXhrStageContract:
    """Test that _capture_xhr_stage populates SiteProfile correctly.

    _capture_xhr_stage uses lazy local imports:
        from .xhr_interceptor import capture_xhr_signals
        from .api_classifier  import classify_captures
        from .api_schema_analyzer import analyze_schema

    Patching must target the *source* modules so the local rebind picks up
    the mock at function-call time.
    """

    @pytest.mark.asyncio
    async def test_populates_xhr_captures_on_success(self):
        from app.services.scraper.site_probe import SiteProfile, _capture_xhr_stage
        from app.services.scraper.xhr_interceptor import XhrCapture
        from app.services.scraper.api_classifier import ClassifiedAPI
        from app.services.scraper.api_schema_analyzer import ApiFieldMapping

        profile = SiteProfile(url="https://example.com", probed_at="2026-01-01T00:00:00Z")
        profile.is_js_spa = True

        cap = XhrCapture(
            url="https://abc.algolia.net/search",
            content_type="application/json",
            body_size=1500,
            sample_body=_ALGOLIA_BODY,
        )
        classified = ClassifiedAPI(
            api_type="algolia",
            endpoint_url="https://abc.algolia.net/search",
            confidence=0.9,
            sample_response=_ALGOLIA_BODY,
            results_path="hits",
        )
        mapping = ApiFieldMapping(
            field_mapping={"course_name": "title", "url": "url"},
            results_path="hits",
            api_type="algolia",
            overall_confidence=0.6,
        )

        # Patch at the source modules — lazy local imports in _capture_xhr_stage
        # rebind from those modules at call time, so this is the correct target.
        with (
            patch(
                "app.services.scraper.xhr_interceptor.capture_xhr_signals",
                AsyncMock(return_value=[cap]),
            ),
            patch(
                "app.services.scraper.api_classifier.classify_captures",
                return_value=classified,
            ),
            patch(
                "app.services.scraper.api_schema_analyzer.analyze_schema",
                return_value=mapping,
            ),
        ):
            await _capture_xhr_stage(profile)

        assert profile.xhr_captures == [cap]
        assert profile.api_field_mapping is mapping
        assert any(a.provider == "algolia" for a in profile.detected_apis)

    @pytest.mark.asyncio
    async def test_no_captures_leaves_profile_clean(self):
        from app.services.scraper.site_probe import SiteProfile, _capture_xhr_stage

        profile = SiteProfile(url="https://example.com", probed_at="2026-01-01T00:00:00Z")
        profile.is_js_spa = True

        with patch(
            "app.services.scraper.xhr_interceptor.capture_xhr_signals",
            AsyncMock(return_value=[]),
        ):
            await _capture_xhr_stage(profile)

        assert profile.xhr_captures == []
        assert profile.api_field_mapping is None
        assert profile.detected_apis == []

    @pytest.mark.asyncio
    async def test_playwright_failure_is_non_fatal(self):
        from app.services.scraper.site_probe import SiteProfile, _capture_xhr_stage

        profile = SiteProfile(url="https://example.com", probed_at="2026-01-01T00:00:00Z")
        profile.is_js_spa = True

        with patch(
            "app.services.scraper.xhr_interceptor.capture_xhr_signals",
            AsyncMock(side_effect=RuntimeError("playwright crash")),
        ):
            await _capture_xhr_stage(profile)

        # profile should be unchanged; no exception raised
        assert profile.api_field_mapping is None


# ── auto_config Phase 4B storage ─────────────────────────────────────────────

class TestAutoConfigPhase4B:
    def _gen(self, profile):
        from app.services.scraper.auto_config_generator import _base_config
        return _base_config(profile)

    def test_field_mapping_stored_in_config(self):
        from app.services.scraper.auto_config_generator import _base_config
        from app.services.scraper.api_schema_analyzer import ApiFieldMapping
        from app.services.scraper.site_probe import SiteProfile
        from unittest.mock import MagicMock

        profile = MagicMock(spec=SiteProfile)
        profile.url = "https://example.com"
        profile.recommended_strategy = "static_html"
        profile.detected_apis = []
        profile.library_stack = None
        profile.is_cloudflare_blocked = False
        profile.is_bot_protected = False
        profile.is_js_spa = False
        profile.has_sitemap = False
        profile.sitemap_course_count = 0
        profile.sitemap_url = None
        profile.wayback_course_count = 0
        profile.notes = []
        profile.strategy_confidence = 0.7
        profile.cms_platform = None
        profile.api_field_mapping = ApiFieldMapping(
            field_mapping={"course_name": "title", "url": "permalink"},
            results_path="hits",
            api_type="algolia",
            overall_confidence=0.6,
        )

        config = _base_config(profile)
        assert config.get("_api_type") == "algolia"
        assert config.get("_field_mapping") == {"course_name": "title", "url": "permalink"}
        assert config.get("_results_path") == "hits"

    def test_no_field_mapping_no_keys(self):
        from app.services.scraper.auto_config_generator import _base_config
        from app.services.scraper.site_probe import SiteProfile
        from unittest.mock import MagicMock

        profile = MagicMock(spec=SiteProfile)
        profile.url = "https://example.com"
        profile.recommended_strategy = "static_html"
        profile.detected_apis = []
        profile.library_stack = None
        profile.is_cloudflare_blocked = False
        profile.is_bot_protected = False
        profile.is_js_spa = False
        profile.has_sitemap = False
        profile.sitemap_course_count = 0
        profile.sitemap_url = None
        profile.wayback_course_count = 0
        profile.notes = []
        profile.strategy_confidence = 0.7
        profile.cms_platform = None
        profile.api_field_mapping = None

        config = _base_config(profile)
        assert "_field_mapping" not in config
        assert "_api_type" not in config
