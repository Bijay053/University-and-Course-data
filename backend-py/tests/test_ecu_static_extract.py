"""Tests for ECU static extractor — is_ecu_course_url and apply_ecu_extraction."""
import pytest
from app.services.scraper.ecu_static_extract import (
    apply_ecu_extraction,
    is_ecu_course_url,
)


# ---------------------------------------------------------------------------
# is_ecu_course_url
# ---------------------------------------------------------------------------

class TestIsEcuCourseUrl:
    """Only /degrees/courses/<single-slug> should return True."""

    def test_valid_undergraduate(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/courses/bachelor-of-science") is True

    def test_valid_postgraduate(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/courses/master-of-business-administration") is True

    def test_valid_no_trailing_slash(self):
        assert is_ecu_course_url("https://ecu.edu.au/degrees/courses/graduate-certificate-in-data-science") is True

    def test_valid_trailing_slash(self):
        # Trailing slash stripped before comparison.
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/courses/bachelor-of-nursing/") is True

    def test_listing_all(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/courses/all") is False

    def test_listing_postgraduate(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/postgraduate") is False

    def test_hub_nested(self):
        # Two-segment path after /degrees/courses/ → category hub, not a course page.
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/courses/health-sciences/bachelor-of-nursing") is False

    def test_article_page(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/news/why-study-at-ecu") is False

    def test_study_experience(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/study/extra/student-life") is False

    def test_non_ecu_host(self):
        assert is_ecu_course_url("https://www.bond.edu.au/degrees/courses/bachelor-of-science") is False

    def test_international_subdomain_not_accepted(self):
        # Only www.ecu.edu.au and ecu.edu.au are accepted.
        assert is_ecu_course_url("https://international.ecu.edu.au/degrees/courses/bachelor-of-science") is False

    def test_empty_string(self):
        assert is_ecu_course_url("") is False

    def test_garbage(self):
        assert is_ecu_course_url("not-a-url") is False

    def test_search_path(self):
        assert is_ecu_course_url("https://www.ecu.edu.au/degrees/courses/search") is False


# ---------------------------------------------------------------------------
# apply_ecu_extraction — has_central_fee_page
# ---------------------------------------------------------------------------

class TestApplyEcuExtractionFeeFlag:
    """has_central_fee_page must always be True."""

    def test_always_true_no_html(self):
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-science", "")
        assert result["has_central_fee_page"] is True

    def test_always_true_with_html(self):
        result = apply_ecu_extraction(
            "https://www.ecu.edu.au/degrees/courses/master-of-business-administration",
            "<html><body>some content</body></html>",
        )
        assert result["has_central_fee_page"] is True


# ---------------------------------------------------------------------------
# apply_ecu_extraction — course_location
# ---------------------------------------------------------------------------

class TestApplyEcuExtractionLocation:
    """Location extraction must detect ECU campuses and reject non-Australian noise."""

    def test_joondalup(self):
        html = "<div>This program is offered at the Joondalup campus.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-nursing", html)
        assert "Joondalup" in result["course_location"]

    def test_mount_lawley(self):
        html = "<div>Study at Mount Lawley campus in Perth.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-arts", html)
        assert "Mount Lawley" in result["course_location"]

    def test_south_west_bunbury(self):
        html = "<div>Available at the South West campus (Bunbury).</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/diploma", html)
        assert "South West" in result["course_location"]

    def test_bunbury_alias(self):
        html = "<div>Offered at our Bunbury campus.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/diploma", html)
        assert "South West" in result["course_location"]

    def test_perth_city_cbd(self):
        html = "<div>Classes held at the Perth City campus (CBD).</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/master-of-law", html)
        assert "Perth City" in result["course_location"]

    def test_multiple_campuses(self):
        html = "<div>Available at Joondalup and Mount Lawley campuses.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-education", html)
        assert "Joondalup" in result["course_location"]
        assert "Mount Lawley" in result["course_location"]

    def test_no_campus_defaults_perth_australia(self):
        html = "<div>No campus information here.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/cert-iv-business", html)
        assert result["course_location"] == "Perth, Australia"

    def test_empty_html_defaults_perth_australia(self):
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-science", "")
        assert result["course_location"] == "Perth, Australia"

    def test_sri_lanka_noise_does_not_win(self):
        """International marketing text containing 'Sri Lanka' must not become the location."""
        html = (
            "<div>Students from Sri Lanka, India and Nepal can apply. "
            "Campus: Joondalup.</div>"
        )
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-nursing", html)
        assert "Sri Lanka" not in result["course_location"]
        assert "Joondalup" in result["course_location"]

    def test_sri_lanka_only_page_defaults_perth(self):
        """When only non-AU noise is present, must fall back to default."""
        html = "<div>Students from Sri Lanka may need to satisfy English requirements.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-science", html)
        assert result["course_location"] == "Perth, Australia"


# ---------------------------------------------------------------------------
# apply_ecu_extraction — fee extraction + scrape_warnings
# ---------------------------------------------------------------------------

class TestApplyEcuExtractionFee:
    """Fee extraction and ecu_fee_review warning logic."""

    def test_fee_extracted_from_html(self):
        html = (
            "<div>International students fee: $36,600 per year. "
            "Local students: $8,301.</div>"
        )
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/master-it", html)
        assert result.get("international_fee") == pytest.approx(36600.0)
        assert result.get("fee_term") == "year"

    def test_fee_extracted_annual_pattern(self):
        html = "<p>Annual tuition: $28,800 for international students.</p>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-science", html)
        assert result.get("international_fee") == pytest.approx(28800.0)

    def test_no_fee_in_html_adds_warning(self):
        html = "<div>Some generic course description with no fee data.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/bachelor-of-science", html)
        assert result.get("international_fee") is None
        assert "ecu_fee_review" in (result.get("scrape_warnings") or [])

    def test_fee_present_no_warning(self):
        html = "<div>Tuition fees for international: $32,400 per year.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/master-education", html)
        assert "ecu_fee_review" not in (result.get("scrape_warnings") or [])

    def test_fee_too_small_ignored(self):
        """Values below $1000 are noise (domestic credit-hours, not annual fees)."""
        html = "<div>International fee: $800 per unit.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/diploma", html)
        assert result.get("international_fee") is None
        assert "ecu_fee_review" in (result.get("scrape_warnings") or [])

    def test_fee_too_large_ignored(self):
        """Values over $200 000 are almost certainly not course fees."""
        html = "<div>International tuition $250,000 for the full program.</div>"
        result = apply_ecu_extraction("https://www.ecu.edu.au/degrees/courses/phd", html)
        assert result.get("international_fee") is None
