"""Generated rule composition must be explicit and source-scoped."""
import pytest

from app.services.scraper.ai_extractor_run import apply_extraction_rules
from app.services.scraper.ai_repair_agent import (
    PatchValidationError, _validate_extraction_patch,
)

PATTERN = r"International:\s*£([\d,]+)"


def rule(**changes):
    return {"css": "#tuitionFees + *", "regex": PATTERN,
            "regex_scope": "selected", "confidence": .95, **changes}


@pytest.mark.parametrize("label", ["International", "Overseas", "Non-UK"])
def test_labelled_fee_is_extracted_inside_value_block(label):
    html = f"""<main><h3 id="tuitionFees">Tuition fee</h3>
    <div>Home: £9,535 {label}: £19,500 per year</div></main>"""
    value, method = apply_extraction_rules(html, {
        "international_fee": rule(regex=label + r":\s*£([\d,]+)")
    })["international_fee"]
    assert value == "19,500" and method == "ai_rule:regex"


@pytest.mark.parametrize("body", [
    "<div>Home: £9,535 International: Fees currently under review</div>",
    "<div>Home: £9,535</div>",
    "<section>No published price</section>",
])
def test_missing_fee_cannot_fall_back_to_other_course_or_page_wide_price(body):
    html = f"""<main><h3 id="tuitionFees">Tuition fee</h3>{body}
    <aside>International: £99,000</aside></main>"""
    assert apply_extraction_rules(html, {"international_fee": rule()})["international_fee"] == (None, "ai_rule:miss")


def test_heading_alone_is_not_a_fee_even_when_other_blocks_contain_price():
    html = '<main><h3 id="tuitionFees">Tuition fee</h3><div>International: £19,500</div></main>'
    assert apply_extraction_rules(html, {"international_fee": rule(css="#tuitionFees")})["international_fee"][0] is None


def test_existing_fallback_rules_keep_their_original_semantics():
    html = '<p id="value">Known selector value</p><p>International: £19,500</p>'
    legacy = {"css": "#value", "regex": PATTERN}
    assert apply_extraction_rules(html, {"description": legacy})["description"] == ("Known selector value", "ai_rule:css")
    legacy["css"] = "#absent"
    assert apply_extraction_rules(html, {"international_fee": legacy})["international_fee"] == ("19,500", "ai_rule:regex")


@pytest.mark.parametrize("invalid", [
    {"regex_scope": "full_page"}, {"regex": ""}, {"css": "", "xpath": ""},
])
def test_invalid_composition_fails_closed_and_cannot_be_saved(invalid):
    candidate = rule(**invalid)
    with pytest.raises(PatchValidationError):
        _validate_extraction_patch("extraction_rules.international_fee", candidate)
    assert apply_extraction_rules(
        '<main>International: £19,500</main>', {"international_fee": candidate},
    )["international_fee"][0] is None


def test_scoped_rule_still_runs_navigation_category_guard(monkeypatch):
    from app.services.scraper import category
    checked = []
    def reject_navigation(value):
        checked.append(value)
        return True
    monkeypatch.setattr(category, "is_navigation_category", reject_navigation)
    html = '<div id="nav">Study International Students Alumni Contact</div>'
    result = apply_extraction_rules(html, {"category": rule(css="#nav", regex=r"(Study International Students Alumni Contact)")})
    assert result["category"][0] is None
    assert checked == ["Study International Students Alumni Contact"]


def test_quoted_context_identifies_the_actual_value_selector():
    from app.services.scraper.ai_repair_live import inspect_page
    html = """<main><h1>Computing MSc</h1>
    <dl><dt>Duration</dt><dd>1 year</dd><dt>Location</dt><dd>City Campus</dd></dl>
    <h3 id="tuitionFees">Tuition fee</h3><div>Home: £9,535 International: £19,500</div></main>"""
    result = inspect_page("https://uni.example/courses/computing", html)
    evidence = next(f for f in result["fields"] if f["selector"] == "h3#tuitionFees")
    assert evidence["selector_text"] == "Tuition fee"
    assert evidence["text_scope"] == "label_and_next_sibling"
    assert evidence["value_selector"] == "h3#tuitionFees + *"
    assert apply_extraction_rules(html, {"international_fee": rule(css=evidence["value_selector"])})["international_fee"][0] == "19,500"


@pytest.mark.parametrize("identity", ["123", "fees:2027"])
def test_non_css_identifiers_do_not_break_course_verification(identity):
    from app.services.scraper.ai_repair_live import inspect_page
    html = f"""<main><h1>Computing MSc</h1>
    <dl><dt>Award</dt><dd>MSc</dd><dt>Duration</dt><dd>1 year</dd><dt>Location</dt><dd>City Campus</dd></dl>
    <h3 id="{identity}">Tuition fee</h3><div>International: £19,500</div></main>"""
    assert inspect_page("https://uni.example/courses/computing", html)["classification"] == "course"
