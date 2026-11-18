"""Large sitemaps supply candidates without claiming catalogue completeness."""
import pytest
from app.services.scraper.repair_sitemap_evidence import inspect_sitemap

URL = "https://university.example/sitemap.xml"
LOC = "https://university.example/courses/engineering"


def inspect(xml, cap=1_000_000, **kwargs):
    return inspect_sitemap(URL, xml, byte_limit=cap, **kwargs)


def test_complete_small_sitemap():
    value = inspect(f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{LOC}</loc></url></urlset>')
    assert value["classification"] == "sitemap"
    assert value["links"] == [{"url": LOC}]
    assert value["coverageComplete"] and value["candidateOnly"]


def test_large_sitemap_keeps_complete_locations_but_not_partial_last_location():
    prefix = f"<urlset><url><loc>{LOC}</loc></url>"
    xml = prefix + "<url><loc>https://university.example/" + "x" * 2000
    value = inspect(xml, cap=256)
    assert value["classification"] == "sitemap"
    assert value["links"] == [{"url": LOC}]
    assert not value["coverageComplete"]
    assert "incomplete" in value["reason"]


def test_complete_locations_are_bounded():
    xml = "<urlset>" + "".join(f"<url><loc>{LOC}/{n}</loc></url>" for n in range(50)) + "</urlset>"
    value = inspect(xml, link_limit=3)
    assert len(value["links"]) == 3
    assert not value["coverageComplete"]


def test_index_is_not_mistaken_for_course_urls():
    value = inspect(f"<sitemapindex><sitemap><loc>{URL}/part.xml</loc></sitemap></sitemapindex>")
    assert value["classification"] == "sitemap_index"
    assert value["candidateOnly"]


def test_image_locations_are_not_course_candidates():
    value = inspect(f'<urlset xmlns:image="https://example.com/image"><url><loc>{LOC}</loc><image:image><image:loc>https://example.com/photo.jpg</image:loc></image:image></url></urlset>')
    assert value["links"] == [{"url": LOC}]


@pytest.mark.parametrize("xml", [
    "<urlset><url><loc>unfinished",
    "<urlset><url><loc>bad</url></loc></urlset>",
    '<!DOCTYPE urlset [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset><url><loc>&x;</loc></url></urlset>',
    "<html><loc>not a sitemap</loc></html>",
    "<urlset>" + "<x>" * 20,
])
def test_malformed_or_unsafe_xml_is_rejected(xml):
    assert inspect(xml)["classification"] == "unconfirmed"


def test_empty_large_prefix_is_not_success():
    value = inspect("<urlset><!--" + "x" * 2000, cap=256)
    assert value["classification"] == "unconfirmed"
    assert not value["links"]


@pytest.mark.asyncio
async def test_fallback_never_certifies_a_sample_as_a_complete_catalogue():
    from types import SimpleNamespace
    from app.services.scraper.config.schema import UniConfig
    from app.services.scraper.official_catalogue_repair import discover_official_catalogue
    home = "https://university.example"
    cfg = UniConfig(slug="test", name="Test", base_url=home, scrape_url=home)
    async def fetch(url):
        if url == URL:
            return {"classification":"sitemap","coverageComplete":False,
                    "links":[{"url":LOC},{"url":LOC + "-msc"}]}
        return {"classification":"unconfirmed","links":[]}
    evidence = SimpleNamespace(ctx={"scrape_url":home,"catalogue_recovery":True},
        config=cfg,extra_hosts=(),fetch=fetch,fetch_seconds=20,max_pages=12)
    result = await discover_official_catalogue(evidence)
    assert result["source"] is None
    assert result["candidates"] == []
    assert len(result["partial_candidates"]) == 2
    assert len(result["sample"]) == 2
