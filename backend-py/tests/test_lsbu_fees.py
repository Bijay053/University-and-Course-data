"""LSBU audience and annual/total authority, including fallback preservation."""
import asyncio

import pytest

from app.services.scraper.extractors import fee
from app.services.scraper.extractors.lsbu_fees import apply_course_fee_authority, course_fee, METHOD

URL = "https://www.lsbu.ac.uk/study/course-finder/beng-hons-civil-engineering"


def card(label, amount):
    return f"""<div class="fees__card">
      <h5 class="fees__card-header-title">{label}</h5>
      <div class="fees__card-content-item">
        <p class="fees__card-subtitle">£{amount}</p>
        <p class="fees__card-info">Tuition fees for
          <a>{'international' if label == 'International' else 'home'} students</a></p>
      </div></div>"""


def table(amount=17400, year=2026, title="Civil Engineering (FT) - Year 1", total=52200):
    return f"""<div class="course-table-wrapper"><h4>{title}</h4>
      <table class="fees__table">
        <tr><td colspan="2">The fee shown is for entry {year}/{str(year + 1)[-2:]}</td></tr>
        <tr><td>UK fee: <span>£9790</span></td>
          <td>International fee: <span>£{amount}</span></td></tr>
      </table>
      <p>Total course fee for this location/stream: UK: £29370 International: £{total}</p>
    </div>"""


def page(price="17400", extra=""):
    return f"""<main><h1>Civil Engineering</h1><section id="fees">
      {card('United Kingdom', '9790')}{card('International', price)}
      {table()}{extra}</section>
      <aside>International scholarship £2500. Home student UK fee £29370.</aside></main>"""


def test_international_card_owns_annual_amount_not_domestic_or_course_total():
    rows = asyncio.run(fee.extract(page(), URL))
    assert len(rows) == 1
    result = rows[0]
    assert result.value == 17400
    assert result.normalized == {
        "international_fee": 17400, "currency": "GBP",
        "fee_term": "Annual", "fee_year": 2026,
    }
    assert result.method == METHOD
    # The configured domestic rejection keywords cannot reject this owned evidence.
    for keyword in ("Home student", "Home fee", "UK student", "UK fee", "Domestic", "per module", "part-time"):
        assert keyword.lower() not in result.snippet.lower()


def test_phd_fee_is_annual_not_the_three_year_total_or_writing_up_price():
    html = page("18200", table(18200, total=54600) + table(1280, title="PhD (FT) - Year 1 (Extension - Writing Up)"))
    _, result = course_fee(html, URL)
    assert result.value == 18200
    assert result.normalized["fee_term"] == "Annual"


@pytest.mark.parametrize("price", ["", "0", "TBC", "17400–20000"])
def test_blank_zero_and_unpublished_international_cards_fail_closed(price):
    known, result = course_fee(page(price), URL)
    assert known and result is None
    assert asyncio.run(fee.extract(page(price), URL)) == []
    payload = {"international_fee": 9790, "currency": "GBP", "fee_term": "Full Course"}
    evidence = [{"field_key": "international_fee", "value": 9790, "method": "ai"}]
    apply_course_fee_authority(page(price), URL, payload, evidence)
    assert payload["international_fee"] is None
    assert payload["fee_term"] is None


def test_missing_headline_falls_back_only_to_latest_full_time_year_one_cell():
    html = f"""<section id="fees">
      {table(16500, 2025)}
      {table(17400, 2026)}
      {table(99999, 2027, "Civil Engineering (PT) - Year 1")}
      {table(88888, 2027, "Civil Engineering (FT) - Year 2")}
      {table(1280, 2027, "Civil Engineering (FT) - Year 1 Extension Writing Up")}
    </section>"""
    _, result = course_fee(html, URL)
    assert result.value == 17400
    assert result.normalized["fee_year"] == 2026


def test_domestic_only_and_conflicting_cards_do_not_become_international_fees():
    domestic = f"<section id='fees'>{card('United Kingdom', '9790')}</section>"
    assert course_fee(domestic, URL) == (True, None)
    assert course_fee(page(extra=card("International", "25000")), URL) == (True, None)


def test_related_fee_cards_outside_current_course_panel_do_not_contribute():
    html = page() + f"<aside>{card('International', '99999')}</aside>"
    assert course_fee(html, URL)[1].value == 17400


def test_final_authority_restores_fee_tuple_after_ai_or_total_replacement():
    payload = {"international_fee": 29370, "currency": "USD", "fee_term": "Full Course", "fee_year": 2024}
    evidence = [{"field_key": "international_fee", "value": 29370, "method": "ai"}]
    apply_course_fee_authority(page(), URL, payload, evidence)
    assert payload["international_fee"] == 17400
    assert payload["currency"] == "GBP"
    assert payload["fee_term"] == "Annual"
    assert payload["fee_year"] == 2026
    assert evidence[-1]["method"] == METHOD
    assert evidence[0]["decision_status"] == "needs_review"


@pytest.mark.asyncio
async def test_full_pipeline_with_lsbu_domestic_rejection_rules_preserves_annual_fee(monkeypatch):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    config = load_uni_config(
        university_id=2175, slug="lsbu", name="London South Bank University",
        scrape_url="https://www.lsbu.ac.uk/", create_missing_stub=False,
    )
    token = current_uni_config.set(config)
    try:
        out = await extract_course(URL, html=page(), country="United Kingdom", use_ai_fallback=False)
    finally:
        current_uni_config.reset(token)
    payload = out["payload"]
    assert payload["international_fee"] == 17400
    assert payload["currency"] == "GBP"
    assert payload["fee_term"] == "Annual"
    assert any(
        row["field_key"] == "international_fee" and row["method"] == METHOD
        and row.get("decision_status") == "selected"
        for row in out["evidence"]
    )


@pytest.mark.parametrize("url", [
    "https://www.example.ac.uk/study/course-finder/civil",
    "https://www.lsbu.ac.uk/fees-and-funding",
    "https://www.lsbu.ac.uk.evil.example/study/course-finder/civil",
])
def test_authority_does_not_change_other_hosts_or_non_course_pages(url):
    assert course_fee(page(), url) == (False, None)