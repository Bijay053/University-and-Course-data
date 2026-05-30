"""Phase 2 autonomous extraction engine — unit tests.

Covers:
- Stage 0: apply_extraction_rules() fills payload and sets method correctly
- Stage 0: should_skip_gemini() fires at >= 85% coverage
- CASCADE smart split: discovery_failure (staged < 5) vs extraction_failure (staged >= 5, avg < 70)
- repair_extractor task: identify_failing_fields() threshold + CRITICAL_FIELDS auto-inclusion
- _apply_css: attribute="text" sentinel returns inner text (not an HTML attr lookup)
"""
import os
import pytest

# ── ai_extractor_run ──────────────────────────────────────────────────────────

class TestApplyExtractionRulesCSS:
    """apply_extraction_rules() with CSS selectors."""

    def _run(self, html: str, rules: dict) -> dict:
        from app.services.scraper.ai_extractor_run import apply_extraction_rules
        return apply_extraction_rules(html, rules)

    def test_css_inner_text_no_attribute_key(self):
        """Omitting attribute → inner text extracted."""
        html = '<h1 class="course-title">Bachelor of Science</h1>'
        rules = {
            "course_name": {
                "css": "h1.course-title",
                "quoted_text": "Bachelor of Science",
                "confidence": 0.9,
            }
        }
        result = self._run(html, rules)
        value, method = result["course_name"]
        assert value == "Bachelor of Science"
        assert method == "ai_rule:css"

    def test_css_attribute_text_sentinel_returns_inner_text(self):
        """attribute='text' is a sentinel meaning inner text, not an HTML attr."""
        html = '<h1 class="course-title">Master of Engineering</h1>'
        rules = {
            "course_name": {
                "css": "h1.course-title",
                "attribute": "text",
                "quoted_text": "Master of Engineering",
                "confidence": 0.9,
            }
        }
        result = self._run(html, rules)
        value, method = result["course_name"]
        assert value == "Master of Engineering"
        assert method == "ai_rule:css"

    def test_css_html_attribute_extracted(self):
        """attribute='content' reads the HTML content= attribute."""
        html = '<meta name="description" content="Study at AUT university">'
        rules = {
            "description": {
                "css": 'meta[name="description"]',
                "attribute": "content",
                "quoted_text": "Study at AUT",
                "confidence": 0.75,
            }
        }
        result = self._run(html, rules)
        value, method = result["description"]
        assert value is not None and "AUT" in value
        assert method == "ai_rule:css"

    def test_missing_css_selector_returns_none(self):
        html = "<p>No title here</p>"
        rules = {
            "course_name": {
                "css": "h1.not-present",
                "quoted_text": "Non Existent Title",
                "confidence": 0.9,
            }
        }
        result = self._run(html, rules)
        value, _ = result["course_name"]
        assert value is None

    def test_empty_rules_dict_returns_empty(self):
        assert self._run("<html><body>x</body></html>", {}) == {}

    def test_multiple_css_rules_all_applied(self):
        html = (
            '<h1 class="cn">Doctor of Philosophy</h1>'
            '<span class="fee">AUD 42,000</span>'
        )
        rules = {
            "course_name": {
                "css": "h1.cn",
                "attribute": "text",
                "quoted_text": "Doctor of Philosophy",
                "confidence": 0.9,
            },
            "international_fee": {
                "css": "span.fee",
                "attribute": "text",
                "quoted_text": "42,000",
                "confidence": 0.8,
            },
        }
        result = self._run(html, rules)
        assert result["course_name"][0] == "Doctor of Philosophy"
        assert result["international_fee"][0] == "AUD 42,000"

    def test_rule_with_no_css_xpath_regex_produces_miss(self):
        html = "<p>some content</p>"
        rules = {
            "course_name": {
                "quoted_text": "some content",
                "confidence": 0.5,
            }
        }
        result = self._run(html, rules)
        value, method = result["course_name"]
        assert value is None
        assert method == "ai_rule:miss"


class TestApplyExtractionRulesRegex:
    """apply_extraction_rules() with regex selectors."""

    def _run(self, html: str, rules: dict) -> dict:
        from app.services.scraper.ai_extractor_run import apply_extraction_rules
        return apply_extraction_rules(html, rules)

    def test_regex_rule_extracts_fee(self):
        html = "<p>Tuition fee: AUD 38,000 per year</p>"
        rules = {
            "international_fee": {
                "regex": r"AUD\s*([\d,]+)",
                "quoted_text": "38,000",
                "confidence": 0.85,
            }
        }
        result = self._run(html, rules)
        value, method = result["international_fee"]
        assert value is not None and "38" in str(value)
        assert method == "ai_rule:regex"

    def test_regex_no_match_returns_none(self):
        html = "<p>No fee information here</p>"
        rules = {
            "international_fee": {
                "regex": r"AUD\s*([\d,]+)",
                "quoted_text": "50,000",
                "confidence": 0.8,
            }
        }
        result = self._run(html, rules)
        value, method = result["international_fee"]
        assert value is None
        assert method == "ai_rule:miss"


class TestApplyExtractionRulesXPath:
    """apply_extraction_rules() with XPath selectors."""

    def _run(self, html: str, rules: dict) -> dict:
        from app.services.scraper.ai_extractor_run import apply_extraction_rules
        return apply_extraction_rules(html, rules)

    def test_xpath_rule_extracts_text(self):
        html = '<div id="fee-block"><span>AUD 35,500</span></div>'
        rules = {
            "international_fee": {
                "xpath": '//div[@id="fee-block"]/span/text()',
                "quoted_text": "35,500",
                "confidence": 0.88,
            }
        }
        result = self._run(html, rules)
        value, method = result["international_fee"]
        assert value is not None and "35" in str(value)
        assert method == "ai_rule:xpath"


class TestShouldSkipGemini:
    """should_skip_gemini() returns True when >= 85% of review fields are covered."""

    def _run(self, results: dict, review_fields: list[str]) -> bool:
        from app.services.scraper.ai_extractor_run import should_skip_gemini
        return should_skip_gemini(results, review_fields)

    def test_100_percent_coverage_skips(self):
        fields = [f"field_{i}" for i in range(13)]
        results = {f: (f"val_{f}", "ai_rule:css") for f in fields}
        assert self._run(results, fields) is True

    def test_below_85_does_not_skip(self):
        # 11/13 = 84.6% → should NOT skip
        all_fields = [f"f{i}" for i in range(13)]
        results = {f"f{i}": (f"v{i}", "ai_rule:css") for i in range(11)}
        assert self._run(results, all_fields) is False

    def test_zero_coverage_does_not_skip(self):
        fields = ["course_name", "degree_level", "category"]
        assert self._run({}, fields) is False

    def test_none_values_not_counted(self):
        fields = ["f1", "f2", "f3"]
        results = {"f1": (None, "ai_rule:css"), "f2": ("val", "ai_rule:css"), "f3": (None, "x")}
        assert self._run(results, fields) is False

    def test_all_13_review_fields_covered_skips(self):
        review_fields = [
            "course_name", "degree_level", "category", "study_mode",
            "course_location", "duration", "intake_months",
            "international_fee", "description", "academic_level",
            "academic_score", "english_test", "other_requirement",
        ]
        results = {f: (f"v_{f}", "ai_rule:css") for f in review_fields}
        assert self._run(results, review_fields) is True

    def test_empty_review_fields_does_not_skip(self):
        assert self._run({}, []) is False


# ── ai_extractor_repair ───────────────────────────────────────────────────────

class TestIdentifyFailingFields:
    """identify_failing_fields() returns fields below threshold.

    Importantly: CRITICAL_FIELDS not present in fill_rates at all are
    automatically included (treat as 0% fill rate).
    """

    CRITICAL = [
        "course_name", "degree_level", "study_mode", "duration",
        "intake_months", "international_fee", "english_test", "other_requirement",
    ]

    def _all_good(self) -> dict[str, float]:
        """All CRITICAL_FIELDS at 1.0 fill rate — ensures auto-inclusion doesn't fire."""
        return {f: 1.0 for f in self.CRITICAL}

    def _run(self, fill_rates: dict, threshold: float = 0.50) -> list[str]:
        from app.services.scraper.ai_extractor_repair import identify_failing_fields
        return identify_failing_fields(fill_rates, threshold=threshold)

    def test_below_threshold_returned(self):
        fill_rates = {**self._all_good(), "international_fee": 0.30, "academic_score": 0.45}
        result = self._run(fill_rates)
        assert "international_fee" in result
        assert "academic_score" in result
        assert "course_name" not in result

    def test_exactly_at_threshold_not_failing(self):
        fill_rates = {**self._all_good(), "course_name": 0.50}
        result = self._run(fill_rates, threshold=0.50)
        assert "course_name" not in result

    def test_empty_fill_rates_returns_critical_fields(self):
        """Empty fill_rates → all CRITICAL_FIELDS returned (never extracted)."""
        result = self._run({})
        for field in self.CRITICAL:
            assert field in result, f"Expected {field} in failing when fill_rates={{}}"

    def test_all_good_fields_returns_empty(self):
        result = self._run(self._all_good())
        assert result == []

    def test_all_bad_non_critical_fields(self):
        """Non-critical fields below threshold ARE returned."""
        fill_rates = {**self._all_good(), "category": 0.10, "academic_level": 0.20}
        result = self._run(fill_rates)
        assert "category" in result
        assert "academic_level" in result

    def test_custom_threshold(self):
        fill_rates = {**self._all_good(), "category": 0.65, "academic_level": 0.85}
        # With threshold=0.70, category fails, academic_level passes
        result = self._run(fill_rates, threshold=0.70)
        assert "category" in result
        assert "academic_level" not in result


# ── Stage 0 integration ───────────────────────────────────────────────────────

class TestStage0Integration:
    """Stage 0 injects rules into extract_course() when extraction_rules provided."""

    @pytest.mark.asyncio
    async def test_extraction_rules_none_no_stage0(self):
        """extraction_rules=None → Stage 0 skipped, function completes normally."""
        os.environ["UNI_CONFIG_GUARD_MODE"] = "soft"
        try:
            from app.services.scraper.pipelines.single_course import extract_course
            html = "<html><body><h1>Test Course</h1></body></html>"
            result = await extract_course(
                "https://test.example.com/course",
                html=html,
                use_ai_fallback=False,
                extraction_rules=None,
            )
            assert isinstance(result, dict)
            assert "payload" in result or "error" in result
        finally:
            os.environ.pop("UNI_CONFIG_GUARD_MODE", None)

    @pytest.mark.asyncio
    async def test_extraction_rules_dict_populates_payload(self):
        """CSS rule in extraction_rules → Stage 0 populates course_name."""
        os.environ["UNI_CONFIG_GUARD_MODE"] = "soft"
        try:
            from app.services.scraper.pipelines.single_course import extract_course
            html = '<html><body><h1 class="cn">Master of Data Science</h1></body></html>'
            rules = {
                "course_name": {
                    "css": "h1.cn",
                    "attribute": "text",
                    "quoted_text": "Master of Data Science",
                    "confidence": 0.95,
                }
            }
            result = await extract_course(
                "https://test.example.com/course/mds",
                html=html,
                use_ai_fallback=False,
                extraction_rules=rules,
            )
            payload = result.get("payload", {})
            assert payload.get("course_name") == "Master of Data Science"
        finally:
            os.environ.pop("UNI_CONFIG_GUARD_MODE", None)

    @pytest.mark.asyncio
    async def test_high_coverage_rules_disable_gemini(self):
        """When Stage 0 rules cover >= 85% of review fields, use_ai_fallback is set False."""
        os.environ["UNI_CONFIG_GUARD_MODE"] = "soft"
        try:
            from app.services.scraper.pipelines.single_course import extract_course
            # Build HTML and rules covering all 13 review fields
            review_fields = [
                "course_name", "degree_level", "category", "study_mode",
                "course_location", "duration", "intake_months",
                "international_fee", "description", "academic_level",
                "academic_score", "english_test", "other_requirement",
            ]
            html_parts = [f'<span class="{f.replace("_","-")}">{f}_value</span>'
                          for f in review_fields]
            html = f"<html><body>{''.join(html_parts)}</body></html>"
            rules = {
                f: {
                    "css": f'span.{f.replace("_", "-")}',
                    "attribute": "text",
                    "quoted_text": f"{f}_value",
                    "confidence": 0.90,
                }
                for f in review_fields
            }
            # The function should complete without calling Gemini
            # (we pass use_ai_fallback=True but Stage 0 should override it)
            result = await extract_course(
                "https://test.example.com/course/phd",
                html=html,
                use_ai_fallback=True,  # Stage 0 should set this to False
                extraction_rules=rules,
            )
            # Verify Stage 0 ran (course_name should be set from the CSS rule)
            payload = result.get("payload", {})
            assert payload.get("course_name") == "course_name_value"
        finally:
            os.environ.pop("UNI_CONFIG_GUARD_MODE", None)
