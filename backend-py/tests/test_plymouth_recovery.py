"""Regression coverage for Plymouth's misrouted sitemap and delivery boilerplate."""
import asyncio

import httpx

from app.services.scraper.discovery import _repair_plymouth_course_routes
from app.services.scraper.extractors import study_mode
from app.services.scraper.guards import is_online_only_for_staging


def test_plymouth_sitemap_rewrites_only_verified_missing_routes(monkeypatch):
    old = "https://www.plymouth.ac.uk/courses/undergraduate/msc-marine-science"
    fixed = "https://www.plymouth.ac.uk/courses/postgraduate/msc-marine-science"
    valid = "https://www.plymouth.ac.uk/courses/undergraduate/msc-integrated-degree"
    missing = "https://www.plymouth.ac.uk/courses/undergraduate/phd-unknown"
    outside = "https://example.edu/courses/undergraduate/msc-marine-science"
    probed = []

    def respond(request):
        probed.append(str(request.url))
        status = {
            old: 404, fixed: 200, valid: 200, missing: 404,
        }.get(str(request.url), 404)
        return httpx.Response(status, request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = asyncio.run(_repair_plymouth_course_routes({
        old: "MSc Marine Science",
        fixed: "MSc Marine Science",
        valid: "Integrated MSc",
        missing: "Unknown PhD",
        outside: "Other university",
    }))
    assert list(result) == [fixed, valid, missing, outside]
    assert not any("postgraduate/msc-integrated-degree" in u for u in probed)
    assert outside not in probed


def test_plymouth_partnership_distance_learning_is_not_course_mode():
    html = """
    <h1>BSc (Hons) Applied Computer Science</h1>
    <div class="module-accordion-body">
      <div>Our Academic Partnerships enable students to enrol for a degree
      at a partnership institution closer to home, or engage in distance
      learning – even when they're in the middle of the ocean.</div>
    </div>
    <p>Study in Plymouth with lab sessions.</p>
    """
    url = "https://www.plymouth.ac.uk/courses/undergraduate/bsc-applied-computer-science"
    assert study_mode.classify_study_mode(html)[0] == "Online"
    assert asyncio.run(study_mode.extract(html, url)) == []
    assert not is_online_only_for_staging("BSc Applied Computer Science", {
        "study_mode": "On Campus", "course_location": "Plymouth",
    }, url)


def test_plymouth_explicit_online_delivery_still_fails_global_gate():
    html = """
    <div>Our Academic Partnerships enable students to engage in distance learning.</div>
    <main><h1>MSc Example</h1><p>Delivery mode: Online</p></main>
    """
    url = "https://www.plymouth.ac.uk/courses/postgraduate/msc-example"
    results = asyncio.run(study_mode.extract(html, url))
    assert results[0].value == "Online"
    assert results[0].method == "study_mode:label"
    assert is_online_only_for_staging("MSc Example", {
        "study_mode": results[0].value, "course_location": "Plymouth",
    }, url)