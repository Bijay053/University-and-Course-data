from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.scraper.extractors.ulaw_campuses import (
    METHOD, apply_course_campus_authority, enrich_course_campuses, parse_course_campuses,
)
from app.services.scraper.extractors.ulaw_fees import METHOD as FEE_METHOD, parse_course_fees

URL = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/"
TITLE = "MSc Healthcare Management"


def fees():
    return """<a role="tab" href="#fees">International Students</a><div id="fees">
    <p>2026/27 Course Fees (for courses starting between 1 July 2026 - 31 May 2027)</p>
    <p>London: £19,050</p><p>Outside London: £17,500</p></div>"""


def page(body):
    return f'<link rel="canonical" href="{URL}"><h1>{TITLE}</h1>{body}{fees()}'


def table(title=TITLE, names=("Birmingham", "London Moorgate"), month="February 2027"):
    return f"""<a role="tab" href="#tab-x-start-dates-0">{month}</a>
    <div id="tab-x-start-dates-0"><h5>{title}</h5><table><tbody><tr><td><ul>
    {''.join(f'<li>{name}</li>' for name in names)}
    </ul></td></tr></tbody></table></div>"""


def parse(html):
    return parse_course_campuses(html, URL, course_name=TITLE, fee_authority=parse_course_fees(html, URL))


def test_course_intake_tables_are_route_and_fee_cohort_owned():
    html = page(table() + '<nav><a href="/locations/bristol/">Bristol</a></nav>')
    result = parse(html)
    assert result["locations"] == ["Birmingham", "London Moorgate"]
    assert result["fee_year"] == 2026
    assert "February 2027" in result["snippet"]


@pytest.mark.parametrize("body", [
    table(title=TITLE + " with Professional Practice"),
    table(month="October 2027"),
    table(names=("Online",)),
    '<nav><a href="/locations/birmingham/">Birmingham</a></nav>',
    table(names=("Outside London",)),
])
def test_unrelated_routes_cohorts_navigation_and_online_fail_closed(body):
    assert parse(page(body)) is None


def test_wrong_url_title_shell_and_mixed_fee_routes_fail_closed():
    html = page(table())
    assert parse(html.replace(URL, URL.replace("healthcare", "marketing"))) is None
    assert parse(html.replace(f"<h1>{TITLE}</h1>", "<h1>Just a moment</h1>")) is None
    authority = parse_course_fees(html, URL)
    authority["selected"][1]["study_variant"] = "Professional Practice"
    assert parse_course_campuses(html, URL, fee_authority=authority) is None
    assert parse_course_campuses(html, "https://example.com/course", fee_authority=authority) is None


def test_key_facts_uses_physical_course_links_not_navigation():
    html = page("""<nav><a href="/locations/bristol/">Bristol</a></nav>
    <section class="key-facts"><div class="key-facts__locations"><h4>Locations</h4>
    <a href="/locations/birmingham/">Birmingham</a>
    <a href="/locations/london/moorgate/">London Moorgate</a>
    <a href="/locations/online/">Online</a>
    <a href="https://example.com/locations/leeds/">Leeds</a></div></section>""")
    assert parse(html)["locations"] == ["Birmingham", "London Moorgate"]


def test_staging_replaces_broad_location_evidence_and_records_authority():
    html = page(table())
    payload = {"course_name": TITLE, "course_location": "Birmingham"}
    evidence = [{"field_key": "course_location", "method": "university_default"},
                {"field_key": "duration", "value": 1}]
    apply_course_campus_authority(html, URL, payload, evidence, parse_course_fees(html, URL))
    assert payload["course_location"] == "Birmingham, London Moorgate"
    assert payload["extraction_method"]["campus_authority"]["source_url"] == URL
    assert [e["method"] for e in evidence if e["field_key"] == "course_location"] == [METHOD]
    assert evidence[0]["field_key"] == "duration"


def staged(html):
    authority = parse_course_fees(html, URL)
    return SimpleNamespace(
        id=1, course_name=TITLE, course_website=URL, course_location="Birmingham",
        international_fee=None, currency="GBP", fee_year=authority["fee_year"],
        fee_term=authority["fee_term"],
        extraction_method={"international_fee": FEE_METHOD, "fee_variants": authority},
    )


def client(monkeypatch, html, status=200):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=html, request=request))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=transport, **kwargs))


@pytest.mark.asyncio
async def test_live_recovery_persists_evidence_without_commit(monkeypatch):
    html = page(table())
    row = staged(html)
    client(monkeypatch, html)
    added = []
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)), add=added.append)
    assert await enrich_course_campuses(db, row) == {"status": "resolved"}
    assert row.course_location == "Birmingham, London Moorgate"
    assert added[0].source_url == URL
    assert added[0].extraction_method == METHOD
    assert row.extraction_method["campus_authority"]["fee_year"] == 2026


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fee", "url", "offline", "shell", "too_large"])
async def test_recovery_failures_do_not_mutate_row(monkeypatch, change):
    html = page(table())
    row = staged(html)
    before = deepcopy(vars(row))
    changed = {
        "fee": html.replace("£19,050", "£20,050"),
        "url": html.replace(URL, URL + "unrelated/"),
        "offline": html,
        "shell": "<h1>Access denied</h1>",
        "too_large": "x" * 2_000_001,
    }[change]
    client(monkeypatch, changed, 503 if change == "offline" else 200)
    result = await enrich_course_campuses(SimpleNamespace(), row)
    assert result["status"] == "needs_review"
    assert result["reason"]
    assert vars(row) == before