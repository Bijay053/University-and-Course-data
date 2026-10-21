import pytest

from app.services.scraper.linked_fee_repair import (
    course_fee_links, published_fee, recover_fee, borrowed_domestic_fee,
)

URL = "https://example.ac.uk/course/psychology/"
COURSE = """<main><h1>BSc (Hons) Psychology</h1>
<dl><dt>Duration</dt><dd>3 years full-time</dd>
<dt>Campus</dt><dd>City Campus</dd></dl>
<h2>Entry requirements</h2><p>IELTS 6.0 overall with 5.5 in each component.</p>
<section><h2>Fees and Funding</h2><a href="/fees/psychology/">Course Fees</a></section>
</main>"""


def table(year, amount, international="International"):
    return f"""<table><tr><th>{year}/{str(year+1)[-2:]}</th><th>UK</th>
    <th>{international}</th></tr>
    <tr><td>Full-time</td><td>£9,790 per level</td><td>{amount} per level</td></tr>
    <tr><td>Part-time</td><td>£81 per credit</td><td>N/A</td></tr>
    <tr><td>Placement Year</td><td>£1,955</td><td>£1,955</td></tr></table>"""


def source(*tables):
    return "<main><h1>Course Fees</h1>" + "".join(tables) + (
        "<p>The full-time tuition fees displayed above are for each year (level) "
        "of the course. International fees quoted are for one year of study.</p></main>"
    )


def test_links_are_course_owned_and_same_host():
    assert course_fee_links(COURSE, URL) == ["https://example.ac.uk/fees/psychology/"]
    assert course_fee_links(COURSE.replace("/fees/psychology/", "https://evil.test/fee"), URL) == []
    assert course_fee_links(COURSE.replace("<section>", "<nav>").replace("</section>", "</nav>"), URL) == []


@pytest.mark.asyncio
async def test_latest_published_international_annual_fee_not_domestic_or_placement():
    value = await published_fee(source(table(2026, "£18,300"), table(2027, "£18,800")), URL)
    assert value is not None
    assert {k: value[k] for k in ("international_fee", "currency", "fee_term", "fee_year")} == {
        "international_fee": 18800, "currency": "GBP", "fee_term": "Annual", "fee_year": 2027,
    }


@pytest.mark.asyncio
async def test_missing_or_conflicting_international_tuition_fails_closed():
    assert await published_fee(source(table(2027, "N/A")), URL) is None
    assert await published_fee(source(table(2027, "£18,800"), table(2027, "£20,000")), URL) is None
    assert await published_fee(source(table(2027, "£18,800", "Home")), URL) is None
    assert await published_fee(source(table(2026, "£18,300"), table(2027, "£TBC")), URL) is None


def test_unpublished_international_fee_never_borrows_domestic_amount():
    html = source(table(2027, "£TBC"))
    assert borrowed_domestic_fee(html, 9790)["unpublished"]
    assert borrowed_domestic_fee(html, 18800) is None
    assert borrowed_domestic_fee(source(table(2027, "£18,800")), 9790) is None


@pytest.mark.asyncio
async def test_research_table_uses_full_time_tuition_not_thesis_pending_or_part_time():
    html = source("""<table><tr><th>2027/28 Entry*</th><th>Full-time</th><th>Part-time</th></tr>
      <tr><td>Home</td><td>£5,390</td><td>£2,695</td></tr>
      <tr><td>International</td><td>£17,200</td><td>£8,600</td></tr>
      <tr><td>Thesis Pending International (MPhil/PhD)</td><td>£2,530</td><td>£2,530</td></tr>
    </table>""")
    assert (await published_fee(html, URL))["international_fee"] == 17200


@pytest.mark.asyncio
async def test_recovery_reads_only_explicit_course_link_and_preserves_provenance():
    fetched = []
    async def fetch(url):
        fetched.append(url)
        return source(table(2027, "£18,800"))
    value = await recover_fee(COURSE, URL, None, fetch)
    assert value is not None
    assert fetched == [value["source_url"]] == ["https://example.ac.uk/fees/psychology/"]


@pytest.mark.asyncio
async def test_live_validation_proves_both_course_links_before_enabling(monkeypatch):
    from types import SimpleNamespace
    from app.services.scraper import ai_repair_live as live
    cfg = SimpleNamespace(
        discovery=SimpleNamespace(allowed_extra_hostnames=[], insecure_tls_direct_hostnames=[]),
        extraction=SimpleNamespace(), filters=SimpleNamespace(),
    )
    second = URL.replace("psychology", "criminology")
    engine = live.LiveRepairEvidence({"effective_config": cfg, "scrape_url": URL})
    engine.discovery_validation = lambda *_: {
        "accepted": True, "reasons": [], "courses": [URL, second],
    }
    async def fetch(url, config, timeout):
        return (source(table(2027, "£18,800")) if "/fees/" in url else COURSE), "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    result = await engine.validate({}, {}, {"linked_fee_repair": True})
    assert result["accepted"], result["reasons"]
    assert len(result["linked_fee_outputs"]) == 2
    assert engine.pages_checked == 3
    assert "_source_html" not in str(engine.audit())
