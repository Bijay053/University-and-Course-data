import pytest

from app.services.scraper.extractors.bedfordshire_fees import course_fee
from app.services.scraper.extractors.fee import extract

URL = "https://www.beds.ac.uk/courses/business-management-with-finance/"


def panel(code, audience, text):
    return (f'<div data-course-admission="{code}" data-study-type="{audience}">'
            f'<div data-id="{audience}"><p>{text}</p></div></div>')


def price(amount="18,150", year="2027/28", level="undergraduate", term="per year"):
    return (f"The full-time standard {level} tuition fee for the academic year "
            f"{year} is £{amount} {term}.")


def page(extra=""):
    return (
        "<h1>Business Management with Finance BSc (Hons)</h1>"
        + panel("BSBFMAAF-02AA-U", "The UK", price("10,050"))
        + panel("BSBFMAAF-02AA-U", "International", price())
        + panel("BSBFMAAF-10AA-U", "International", price())
        + panel("BSMFFAAF-02AA-U", "The UK",
                "The full-time standard fee for this Foundation Degree for the "
                "Academic Year 2027/28 is £5,760. These are the full course fees.")
        + panel("BSMFFAAF-02AA-U", "International",
                "International students cannot apply for Foundation Degrees.")
        + extra
    )


@pytest.mark.asyncio
async def test_real_template_does_not_use_uk_or_foundation_degree_price():
    result = await extract(page(), URL, country="United Kingdom")
    assert len(result) == 1
    assert result[0].normalized == {
        "international_fee": 18150, "currency": "GBP",
        "fee_year": 2027, "fee_term": "Annual",
    }
    assert result[0].method == "fee.bedfordshire_admission_audience"
    assert "5,760" not in result[0].snippet and "10,050" not in result[0].snippet


def test_latest_published_year_not_the_largest_amount():
    _, result = course_fee(page(panel("older", "International", price("20,000", "2026/27"))), URL)
    assert result.value == 18150 and result.normalized["fee_year"] == 2027


@pytest.mark.parametrize("html", [
    page(panel("other-offering", "International", price("19,000"))),
    page().replace("BSc (Hons)", "FdA"),
    page().replace("BSc (Hons)", "MRes"),
    "<h1>Business Management BSc (Hons)</h1>" + panel("home", "The UK", price("10,050")),
    "<h1>Business Management BSc (Hons)</h1>" + panel("mixed", "Home International", price()),
    "<h1>Business Management BSc (Hons)</h1>" + panel("intl", "International",
        '<div data-study-type="The UK"><p>' + price("10,050") + "</p></div>"),
    "<h1>Business Management BSc (Hons)</h1>" + panel("intl", "International",
        '<div data-course-admission="other"><p>' + price("10,050") + "</p></div>"),
    "<h1>Business Management BSc (Hons)</h1>" + panel("intl", "International",
        price().replace("tuition fee", "tuition deposit")),
])
@pytest.mark.asyncio
async def test_ambiguous_or_inapplicable_panels_never_fall_back_to_domestic_price(html):
    assert await extract(html, URL, country="United Kingdom") == []


def test_unknown_query_option_is_not_silently_combined():
    assert course_fee(page(), URL + "?admission=unknown") == (True, None)


def test_other_universities_are_unchanged():
    assert course_fee(page(), URL.replace("www.beds.ac.uk", "example.edu")) == (False, None)


def test_integrated_masters_do_not_borrow_postgraduate_taught_fees():
    html = page().replace("Business Management with Finance BSc (Hons)", "Engineering MEng (Hons)")
    _, result = course_fee(html, URL)
    assert result.value == 18150


def test_taught_postgraduate_fee_keeps_its_explicit_full_course_basis():
    html = "<h1>Computing MSc</h1>" + panel("MSc-1", "International",
        price("18,750", level="postgraduate", term="for the full course"))
    _, result = course_fee(html, URL.replace("business-management-with-finance", "computing"))
    assert result.value == 18750 and result.normalized["fee_term"] == "Full Course"
