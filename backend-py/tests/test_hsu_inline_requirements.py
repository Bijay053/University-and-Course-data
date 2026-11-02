from app.services.scraper.extractors.hsu_facts import apply_course_authority
import pytest

URL = "https://www.hsu.ac.uk/course/master-of-chiropractic/"


def extract(html):
    payload, evidence = {}, []
    apply_course_authority(html, URL, payload, evidence)
    return payload, evidence


def test_master_inline_price_and_implicit_ielts():
    payload, evidence = extract("""
    <h3>Tuition Fees 2026/27</h3><p>UK/ROI – <strong>£9,790</strong><br>
    International – <strong>£17, 684</strong></p>
    <details><summary>International Students</summary><p>Undergraduate standard:
    6.0 overall with no less than 5.5 in each component.</p></details>""")
    assert payload["international_fee"] == 17684
    assert payload["fee_term"] == "Annual"
    assert payload["fee_year"] == 2026
    assert payload["ielts_overall"] == 6
    for part in ("reading", "writing", "listening", "speaking"):
        assert payload[f"ielts_{part}"] == 5.5
        assert any(e["field_key"] == f"ielts_{part}" for e in evidence)


@pytest.mark.parametrize("text,overall,band", [
    ("ILETS overall score of 6.5 with no less than 6.0 in each component.", 6.5, 6),
    ("IELTS (Academic) 7.0 overall, with no component below 6.5.", 7, 6.5),
    ("IELTS (academic) score of 7.0 or above with no less than 6.0 in each section.", 7, 6),
    ("An overall score of 7.0 overall (with no element below 6.5) for IELTS.", 7, 6.5),
])
def test_course_specific_english(text, overall, band):
    p, _ = extract(f"<details><summary>International Students</summary><p>{text}</p></details>")
    assert p["ielts_overall"] == overall
    assert p["ielts_writing"] == band


def test_undated_fee_is_not_given_invented_period():
    p, _ = extract("<h3>Tuition Fees</h3><p>International – £15,500.</p>")
    assert p["international_fee"] == 15500
    assert p["fee_year"] is None
    assert p["fee_term"] is None


def test_no_band_inference_from_overall_only():
    p, _ = extract("<details><summary>International Students</summary><p>IELTS level 7.0.</p></details>")
    assert "ielts_writing" not in p


def test_nested_fee_heading_and_additional_costs():
    p, _ = extract("""<div><h4>Tuition Fees 2026/27</h4>
    <h4>International</h4><p>£17,684</p><h4>Additional Course Costs</h4>
    <p>£200</p></div>""")
    assert p["international_fee"] == 17684


@pytest.mark.parametrize("body,amount,term", [
    ("The current course costs for the Postgraduate Research Degree course 2026/27 entry* "
     "are per annum and shown below. Home £4,988 Full-time International "
     "£9,143 Part-time £17, 046 Full-time", 17046, "Annual"),
    ("2026/27 £9,200 These are the fees for the full MSc "
     "International 2026/27 £17,520 These are the fees for the full MSc (180 credits) "
     "£973 per 10 credit", 17520, "Full Course"),
])
def test_prose_year_fee_templates(body, amount, term):
    p, _ = extract(f"<section><h2>Course Fees</h2><div>{body}</div></section>")
    assert (p["international_fee"], p["fee_term"], p["fee_year"]) == (amount, term, 2026)
