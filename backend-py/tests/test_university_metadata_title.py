import pytest
from types import SimpleNamespace

from app.services.scraper import http_fetcher
from app.routers.universities import (
    _campus_index_links,
    _campus_page_links,
    _campus_page_location,
    _contains_encoded_html_entity,
    _decode_metadata_text,
    _extract_structured_locations,
    _metadata_title_segments,
    _hostname_fallback_label,
    _fetch_onboarding_homepage,
    _is_hostname_fallback_name,
    _is_onboarding_challenge,
    _normalise_metadata_locality,
    _upsert_discovered_locations,
)


@pytest.mark.asyncio
async def test_blocked_homepage_escalates_to_rendered_metadata(monkeypatch) -> None:
    class Response:
        status_code = 403
        text = "<title>Just a moment...</title>"

    class Client:
        async def get(self, _url: str) -> Response:
            return Response()

    calls: list[dict] = []

    async def fake_rendered_fetch(url: str, **kwargs) -> str:
        calls.append({"url": url, **kwargs})
        return "<title>SEGi University &amp; Colleges</title>"

    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", fake_rendered_fetch)

    html = await _fetch_onboarding_homepage(
        Client(),
        "https://www.segi.edu.my",
    )

    assert "SEGi University" in html
    assert calls == [{
        "url": "https://www.segi.edu.my",
        "render": True,
        "wait_for_ms": 2500,
        "rate_limit": False,
        "max_retries": 1,
    }]


@pytest.mark.asyncio
async def test_refreshes_unverified_discovered_location_metadata() -> None:
    existing = SimpleNamespace(
        display_name="SEGi University & Colleges",
        full_address="Old address",
        city="Kota Damansara PJU 5, Petaling Jaya,",
        state_region="Selangor",
        country="MY",
        latitude=None,
        longitude=None,
        is_verified=False,
    )

    class Result:
        def scalars(self):
            return [existing]

    class Db:
        async def execute(self, _statement):
            return Result()

        def add(self, _row):
            raise AssertionError("Matching location should be refreshed, not added")

    changed = await _upsert_discovered_locations(
        Db(),
        13,
        [{
            "display_name": "SEGi University & Colleges",
            "full_address": "No 9, Jalan Teknologi, Kota Damansara, Malaysia",
            "city": "Kota Damansara",
            "state_region": "Selangor",
            "country": "Malaysia",
            "latitude": 3.15,
            "longitude": 101.58,
        }],
        "Malaysia",
    )

    assert changed == 1
    assert existing.city == "Kota Damansara"
    assert existing.country == "Malaysia"
    assert existing.latitude == 3.15


@pytest.mark.asyncio
async def test_does_not_overwrite_verified_location_metadata() -> None:
    existing = SimpleNamespace(
        display_name="Main Campus",
        full_address="Operator-corrected address",
        city="Petaling Jaya",
        state_region="Selangor",
        country="Malaysia",
        latitude=3.1,
        longitude=101.5,
        is_verified=True,
    )

    class Result:
        def scalars(self):
            return [existing]

    class Db:
        async def execute(self, _statement):
            return Result()

        def add(self, _row):
            raise AssertionError("Verified location should not be added again")

    changed = await _upsert_discovered_locations(
        Db(),
        13,
        [{
            "display_name": "Main Campus",
            "full_address": "Website changed address",
            "city": "Kota Damansara",
            "state_region": "Selangor",
            "country": "Malaysia",
            "latitude": 4.0,
            "longitude": 102.0,
        }],
        "Malaysia",
    )

    assert changed == 0
    assert existing.full_address == "Operator-corrected address"
    assert existing.city == "Petaling Jaya"


def test_decodes_html_entities_in_metadata_text() -> None:
    assert _decode_metadata_text(
        "INTI International University&amp; Colleges"
    ) == "INTI International University & Colleges"


def test_decodes_numeric_dash_before_splitting_seo_title() -> None:
    assert _metadata_title_segments(
        "INTI International University &amp; Colleges &#8211; Your Future Built Today"
    ) == [
        "INTI International University & Colleges",
        "Your Future Built Today",
    ]


def test_normalizes_metadata_whitespace() -> None:
    assert _decode_metadata_text("Example&nbsp;&nbsp; University\n") == "Example University"


def test_detects_cloudflare_onboarding_challenge() -> None:
    assert _is_onboarding_challenge(403, "<title>Just a moment...</title>")
    assert _is_onboarding_challenge(
        200,
        "<title>Just a moment...</title><div class='cf-chl-widget'></div>",
    )
    assert not _is_onboarding_challenge(
        200,
        "<title>SEGi University &amp; Colleges</title>",
    )


def test_identifies_hostname_derived_names_for_later_repair() -> None:
    assert _hostname_fallback_label("www.segi.edu.my") == "Segi"
    assert _is_hostname_fallback_name("Segi", "www.segi.edu.my")
    assert not _is_hostname_fallback_name(
        "SEGi University & Colleges",
        "www.segi.edu.my",
    )


def test_normalizes_compound_malaysian_locality_for_header() -> None:
    assert _normalise_metadata_locality(
        "Kota Damansara PJU 5, Petaling Jaya,"
    ) == "Kota Damansara"


def test_detects_encoded_entities_in_existing_university_name() -> None:
    assert _contains_encoded_html_entity(
        "INTI International University &amp; Colleges &#8211; Your Future Built Today"
    )
    assert not _contains_encoded_html_entity(
        "INTI International University & Colleges"
    )


def test_extracts_multiple_jsonld_campus_locations() -> None:
    page_html = """
    <script type="application/ld+json">
    {
      "@type": "CollegeOrUniversity",
      "name": "Example University",
      "location": [
        {
          "@type": "Place",
          "name": "City Campus",
          "address": {
            "streetAddress": "1 Main Street",
            "addressLocality": "Sydney",
            "addressRegion": "NSW",
            "postalCode": "2000",
            "addressCountry": "Australia"
          }
        },
        {
          "@type": "Place",
          "name": "North Campus",
          "address": {
            "addressLocality": "North Sydney",
            "addressRegion": "NSW",
            "addressCountry": {"name": "Australia"}
          }
        }
      ]
    }
    </script>
    """

    assert _extract_structured_locations(page_html) == [
        {
            "display_name": "City Campus",
            "full_address": "1 Main Street, Sydney, NSW, 2000, Australia",
            "city": "Sydney",
            "state_region": "NSW",
            "country": "Australia",
        },
        {
            "display_name": "North Campus",
            "full_address": "North Sydney, NSW, Australia",
            "city": "North Sydney",
            "state_region": "NSW",
            "country": "Australia",
        },
    ]


def test_deduplicates_repeated_jsonld_locations() -> None:
    page_html = """
    <script type="application/ld+json">
    {"@graph": [
      {"name": "Main Campus", "address": {"addressLocality": "Perth"}},
      {"name": "Main Campus", "address": {"addressLocality": "Perth"}}
    ]}
    </script>
    """
    assert len(_extract_structured_locations(page_html)) == 1


def test_prefers_branded_jsonld_owner_and_expands_country_code() -> None:
    page_html = """
    <script type="application/ld+json">
    {"@graph": [
      {
        "@type": "Place",
        "address": {
          "streetAddress": "No 9, Jalan Teknologi",
          "addressLocality": "Kota Damansara",
          "addressRegion": "Selangor",
          "postalCode": "47810",
          "addressCountry": "MY"
        }
      },
      {
        "@type": "CollegeOrUniversity",
        "name": "SEGi University & Colleges",
        "address": {
          "streetAddress": "No 9, Jalan Teknologi",
          "addressLocality": "Kota Damansara",
          "addressRegion": "Selangor",
          "postalCode": "47810",
          "addressCountry": "MY"
        }
      }
    ]}
    </script>
    """

    assert _extract_structured_locations(page_html) == [{
        "display_name": "SEGi University & Colleges",
        "full_address": (
            "No 9, Jalan Teknologi, Kota Damansara, Selangor, 47810, Malaysia"
        ),
        "city": "Kota Damansara",
        "state_region": "Selangor",
        "country": "Malaysia",
    }]


def test_preserves_full_address_and_coordinates_while_cleaning_city() -> None:
    page_html = """
    <script type="application/ld+json">
    {
      "@type": "CollegeOrUniversity",
      "name": "SEGi University & Colleges",
      "geo": {
        "latitude": "3.150150354886943",
        "longitude": "101.5811306094481"
      },
      "address": {
        "streetAddress": "No 9, Jalan Teknologi,",
        "addressLocality": "Kota Damansara PJU 5, Petaling Jaya,",
        "addressRegion": "Selangor Darul Ehsan",
        "postalCode": "47810",
        "addressCountry": "MY"
      }
    }
    </script>
    """

    assert _extract_structured_locations(page_html) == [{
        "display_name": "SEGi University & Colleges",
        "full_address": (
            "No 9, Jalan Teknologi, Kota Damansara PJU 5, Petaling Jaya, "
            "Selangor Darul Ehsan, 47810, Malaysia"
        ),
        "city": "Kota Damansara",
        "state_region": "Selangor Darul Ehsan",
        "country": "Malaysia",
        "latitude": 3.150150354886943,
        "longitude": 101.5811306094481,
    }]


def test_discovers_unique_campus_detail_pages() -> None:
    page_html = """
    <a href="/campuses/">All campuses</a>
    <a href="/campuses/city-campus/">City</a>
    <a href="/our-campuses/city-campus/">City duplicate</a>
    <a href="/campuses/north-campus/">North</a>
    <a href="/campuses/education-counselling-centres/">Counselling</a>
    """
    assert _campus_page_links(page_html, "https://example.edu") == [
        "https://example.edu/campuses/city-campus/",
        "https://example.edu/campuses/north-campus/",
    ]


def test_discovers_nested_generic_location_paths_and_rejects_external_links() -> None:
    page_html = """
    <a href="/about/locations/london-campus/">London</a>
    <a href="/study/campus/city-centre/">City Centre</a>
    <a href="https://other.edu/locations/other-campus/">Other</a>
    """
    assert _campus_page_links(page_html, "https://example.edu") == [
        "https://example.edu/about/locations/london-campus/",
        "https://example.edu/study/campus/city-centre/",
    ]


def test_discovers_campus_and_location_index_pages() -> None:
    page_html = """
    <a href="/about/campuses/">Campuses</a>
    <a href="/locations/">Locations</a>
    <a href="https://other.edu/campuses/">External</a>
    """
    assert _campus_index_links(page_html, "https://example.edu") == [
        "https://example.edu/about/campuses/",
        "https://example.edu/locations/",
    ]


def test_extracts_address_and_coordinates_from_campus_page() -> None:
    page_html = """
    <html>
      <head><title>INTI International University &#8211; INTI Colleges</title></head>
      <body>
        <div class="campus-location">
          Persiaran Perdana BBN, Putra Nilai, 71800 Nilai,
          Negeri Sembilan, Malaysia
        </div>
        <iframe src="https://maps.example/embed?!2d101.75847!3d2.81333"></iframe>
      </body>
    </html>
    """
    assert _campus_page_location(
        page_html,
        "https://newinti.edu.my/campuses/inti-international-university/",
    ) == {
        "display_name": "INTI International University",
        "full_address": (
            "Persiaran Perdana BBN, Putra Nilai, 71800 Nilai, "
            "Negeri Sembilan, Malaysia"
        ),
        "city": "Nilai",
        "state_region": "Negeri Sembilan",
        "country": "Malaysia",
        "latitude": 2.81333,
        "longitude": 101.75847,
    }


def test_extracts_city_and_region_when_campus_address_omits_country() -> None:
    page_html = """
    <html>
      <head><title>INTI College Sabah – INTI Colleges</title></head>
      <body>
        <div class="campus-location">
          Batu 2.5, Jalan Tuaran, 88450 Kota Kinabalu, Sabah.
        </div>
      </body>
    </html>
    """
    location = _campus_page_location(page_html, "https://example.edu/campuses/sabah/")
    assert location is not None
    assert location["city"] == "Kota Kinabalu"
    assert location["state_region"] == "Sabah"
    assert location["country"] is None


def test_finds_postcode_locality_after_commas_inside_street_address() -> None:
    page_html = """
    <html>
      <head><title>INTI College Subang – INTI Colleges</title></head>
      <body>
        <div class="campus-location">
          No. 3, Jalan SS 15/8, (Lot 29, 31, Jalan Subang Utama)
          47500 Subang Jaya, Selangor
        </div>
      </body>
    </html>
    """
    location = _campus_page_location(page_html, "https://example.edu/campuses/subang/")
    assert location is not None
    assert location["city"] == "Subang Jaya"
    assert location["state_region"] == "Selangor"
    assert location["country"] is None