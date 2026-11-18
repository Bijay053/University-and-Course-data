"""Candidate preferences must not turn support pages into course evidence."""
import pytest
from app.services.scraper.official_catalogue_repair import (
    _sitemap_candidate_rank, _sitemap_sample, _bounded_sitemap_urls,
)
from app.services.scraper.repair_sitemap_evidence import inspect_sitemap

HOME = "https://www.tru.ca"
ROOT = HOME + "/arts/programs/master-of-arts-in-human-rights-and-social-justice"


@pytest.mark.parametrize("leaf", [
    "faculty.html", "resources.html", "tuition.html", "student-resources.html",
    "contact.html", "msc-committee.html",
])
def test_award_ancestor_does_not_promote_support_leaf(leaf):
    support = ROOT + "/" + leaf
    course = ROOT + ".html"
    assert _sitemap_candidate_rank(support)[0] == 4
    assert _sitemap_sample([support, course], {}, 1) == [course]


def test_course_namespace_is_preferred_without_claiming_page_verification():
    details = [HOME + "/programs/catalogue/bachelor-of-computing-science.html",
               HOME + "/programs/catalogue/master-of-science.html"]
    other = HOME + "/distance/programs/bachelor-of-commerce.html"
    assert set(_sitemap_sample([other, ROOT + "/faculty.html", *details], {}, 2)) == set(details)


def test_candidate_cap_is_applied_after_ranking_not_an_alphabetical_prefix():
    noise = [HOME + f"/arts/programs/master-of-arts-{i}/faculty.html" for i in range(220)]
    courses = [HOME + "/programs/catalogue/bachelor-of-science.html",
               HOME + "/programs/catalogue/master-of-education.html"]
    selected = _bounded_sitemap_urls([*noise, *courses], {}, 200)
    assert len(selected) == 200 and all(url in selected for url in courses)


def test_same_host_https_upgrade_preserves_published_source_and_deduplicates():
    legacy = "http://www.tru.ca/programs/catalogue/bachelor-of-science.html"
    secure = legacy.replace("http:", "https:")
    xml = "<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in [legacy,secure]) + "</urlset>"
    result = inspect_sitemap(HOME + "/sitemap.xml", xml, byte_limit=10000)
    assert result["links"] == [{"url":secure,"publishedUrl":legacy}]
    assert result["candidateOnly"] and result["coverageComplete"]


def test_normal_discovery_uses_the_same_secure_location_as_repair():
    import re
    from app.services.scraper.sitemap import _normalize_sitemap_url
    legacy = "http://www.tru.ca/programs/catalogue/bachelor-of-science.html"
    assert _normalize_sitemap_url(legacy, official_origin=HOME,
        allow_url_patterns=[re.compile(r"^https://www\.tru\.ca/programs/catalogue/")]) == legacy.replace("http:", "https:")
    assert _normalize_sitemap_url(legacy) == legacy
    for patterns in ([], [re.compile(r"^https?://")], [re.compile(r"^http://")]):
        assert _normalize_sitemap_url(legacy, official_origin=HOME,
            allow_url_patterns=patterns) == legacy


@pytest.mark.asyncio
async def test_normal_sitemap_discovery_accepts_the_https_family_verified_by_repair(monkeypatch):
    import re
    from app.services.scraper import sitemap
    paths = ["/programs/catalogue/bachelor-of-science.html",
             "/programs/catalogue/master-of-education.html"]
    xml = "<urlset>" + "".join(
        f"<url><loc>{HOME.replace('https:', 'http:')}{p}</loc></url>" for p in paths
    ) + "</urlset>"
    async def fetch(url,*args,**kwargs):
        return xml if url == HOME+"/sitemap.xml" else ""
    monkeypatch.setattr(sitemap,"_fetch_text",fetch)
    rows = await sitemap.discover_from_sitemap(
        HOME,sitemap_url=HOME+"/sitemap.xml",
        allow_url_patterns=[re.compile(r"^https://www\.tru\.ca/programs/catalogue/[^/?#]+$")],
    )
    assert {r["url"] for r in rows} == {HOME+p for p in paths}


@pytest.mark.parametrize("url", [
    "http://unrelated.example/programs/science.html",
    "http://www.tru.ca.evil.example/programs/science.html",
    "http://user@www.tru.ca/programs/science.html",
    "http://www.tru.ca:8080/programs/science.html",
])
def test_https_upgrade_never_rewrites_unrelated_hosts_credentials_or_ports(url):
    xml = f"<urlset><url><loc>{url}</loc></url></urlset>"
    result = inspect_sitemap(HOME + "/sitemap.xml", xml, byte_limit=10000)
    assert result["links"] == [{"url":url}]


def test_labelled_credential_and_spelled_length_are_positive_course_evidence():
    from app.services.scraper.ai_repair_live import inspect_page
    page = """<main><h1>Bachelor of Business Administration</h1>
    <dl><dt>Credential</dt><dd>Bachelor of Business Administration (General)</dd>
    <dt>Length</dt><dd>Four years</dd><dt>Delivery</dt><dd>Kamloops</dd></dl>
    </main>"""
    result = inspect_page(HOME + "/programs/catalogue/bachelor-business.html", page)
    assert result["classification"] == "course"
    assert any(f["text"] == "Credential: Bachelor of Business Administration (General)"
               for f in result["fields"])


def test_word_length_or_unlabelled_award_does_not_establish_a_course():
    from app.services.scraper.ai_repair_live import inspect_page
    page = "<main><h1>Our faculty</h1><p>We offer a Bachelor degree.</p><p>Length: Four characters</p></main>"
    result = inspect_page(HOME + "/about/faculty.html", page)
    assert result["classification"] != "course"


def test_faculty_page_cannot_borrow_parent_course_facts_from_sidebar():
    from app.services.scraper.ai_repair_live import inspect_page
    page = """<main><h1>Faculty</h1><dl><dt>Credential</dt><dd>Master of Arts</dd>
    <dt>Length</dt><dd>Two years</dd></dl></main>"""
    result = inspect_page(ROOT + "/faculty.html", page)
    assert result["classification"] == "non_course"


@pytest.mark.asyncio
async def test_probe_uses_observed_programme_details_before_support_links(monkeypatch):
    from app.services.scraper import ai_repair_live as live
    from app.services.scraper.config.schema import UniConfig
    from app.services import worker_fencing
    from unittest.mock import AsyncMock
    details=[HOME + "/programs/catalogue/bachelor-of-science.html",
             HOME + "/programs/catalogue/master-of-science.html"]
    support=[ROOT + "/" + leaf for leaf in ["faculty.html","resources.html","tuition.html"]]
    xml="<urlset>"+"".join(f"<url><loc>{u.replace('https:','http:')}</loc></url>"
                         for u in [*support,*details])+"</urlset>"
    body=lambda u: ("<main><h1>Degree</h1><dl><dt>Credential</dt><dd>Master of Science</dd>"
                    "<dt>Length</dt><dd>Two years</dd></dl></main>")
    seen=[]
    async def fetch(url,*args):
        seen.append(url)
        return (xml if url.endswith("sitemap.xml") else body(url) if url in details
                else "<main><h1>University</h1></main>"),None,None
    monkeypatch.setattr(live,"_fetch_official",fetch)
    monkeypatch.setattr(worker_fencing,"reserve_live_fetch",AsyncMock(side_effect=lambda p,s,t:t))
    monkeypatch.setattr(worker_fencing,"refund_live_fetch",AsyncMock())
    cfg=UniConfig(slug="tru-probe",name="University",base_url=HOME,scrape_url=HOME,
                  discovery={"sitemap_url":HOME+"/sitemap.xml"})
    e=live.LiveRepairEvidence({"scrape_url":HOME,"effective_config":cfg,"repair_url_sample":[]})
    await e.probe()
    assert all(url in seen for url in details)
    assert all(seen.index(course) < seen.index(url)
               for course in details for url in support if url in seen)
    assert e.audit()["course_pages"] >= 2 and e.pages_checked <= e.max_pages//2
