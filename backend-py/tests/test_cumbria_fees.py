import asyncio
import pytest
from app.services.scraper.extractors import fee
from app.services.scraper.extractors.cumbria_fees import course_fee

URL = "https://www.cumbria.ac.uk/study/courses/undergraduate/bsc-hons-psychology-counselling-and-psychotherapy/integrated-foundation-year/"


def card(year, amount):
    return f'<div class="internationalfees"><p>{amount}</p><p>Entry in academic year {year}/{str(year+1)[-2:]}</p></div>'


def page(cards, extra=""):
    return f'<main class="course-page"><h1>Psychology with Integrated Foundation Year</h1><div id="ukFeesInfo">£9,790 Entry in academic year 2026/27</div><div id="intFeesInfo">{cards}</div>{extra}</main>'


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("latest", [16000, 14000])
def test_latest_international_year_not_first_or_largest_amount(reverse, latest):
    cards = [card(2025, "£14,900"), card(2026, f"£{latest:,}")]
    result = asyncio.run(fee.extract(page("".join(cards[::-1] if reverse else cards)), URL))
    assert len(result) == 1
    assert result[0].normalized == {
        "international_fee": latest, "currency": "GBP", "fee_term": "Annual", "fee_year": 2026,
    }


def test_newer_unpublished_fee_does_not_invent_a_year_or_amount():
    result = asyncio.run(fee.extract(page(card(2026, "£16,000") + card(2027, "To be confirmed")), URL))
    assert result[0].normalized["fee_year"] == 2026
    assert result[0].value == 16000


def test_conflicting_latest_copies_do_not_fall_back_to_old_or_home_fee():
    html = page(card(2025, "£14,900") + card(2026, "£16,000"),
                '<div id="intFeesDetails"><div class="rightFeesColumn">£17,000 Entry in academic year 2026/27</div></div>')
    assert asyncio.run(fee.extract(html, URL)) == []


def test_identical_responsive_and_tab_prices_are_safe():
    html = page(card(2026, "£16,000"),
                '<div id="intFeesDetails"><div class="rightFeesColumn">£16,000 Entry in academic year 2026/27</div></div>')
    assert asyncio.run(fee.extract(html, URL))[0].value == 16000


@pytest.mark.parametrize("cards", ["", card(2027, "To be confirmed"), card(2026, "£0"), '<p>£16,000</p>'])
def test_no_dated_international_price_never_falls_through_to_domestic(cards):
    assert asyncio.run(fee.extract(page(cards), URL)) == []


def test_does_not_claim_another_institution():
    assert course_fee(page(card(2026, "£16,000")), URL.replace("cumbria.ac.uk", "example.edu")) == (False, None)
