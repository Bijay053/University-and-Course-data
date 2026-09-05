import pytest
from types import SimpleNamespace

from app.services.scraper import http_fetcher
from app.routers.universities import (
    _can_upgrade_to_official_name,
    _campus_index_links,
    _campus_page_links,
    _campus_page_location,
    _contains_encoded_html_entity,
    _decode_metadata_text,
    _extract_structured_locations,
    _metadata_title_segments,
    _onboarding_ai_evidence,
    _hostname_fallback_label,
    _has_generic_title_prefix,
    _fetch_onboarding_homepage,
    _is_hostname_fallback_name,
    _institution_domain,
    _is_onboarding_challenge,
    _normalise_metadata_locality,
    _resolve_university_identity_openai,
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


def test_splits_plain_hyphen_homepage_title() -> None:
    assert _metadata_title_segments(
        "Home - Charles Sturt University"
    ) == ["Home", "Charles Sturt University"]
    assert _has_generic_title_prefix("Home - Charles Sturt University")
    assert not _has_generic_title_prefix("Charles Sturt University")


def test_campus_index_allows_equivalent_institution_subdomain() -> None:
    html = """
    <a href="https://study.csu.edu.au/why-charles-sturt/locations">
      View our campuses
    </a>
    <a href="https://external.example.edu.au/locations">Not ours</a>
    """
    assert _campus_index_links(html, "https://www.csu.edu.au") == [
        "https://study.csu.edu.au/why-charles-sturt/locations/"
    ]


def test_official_campus_links_outrank_partner_study_locations() -> None:
    html = """
    <a href="https://study.csu.edu.au/why-charles-sturt/locations/holmesglen">
      Holmesglen
    </a>
    <a href="https://about.csu.edu.au/locations/campuses/bathurst">
      Bathurst
    </a>
    """
    assert _campus_page_links(html, "https://study.csu.edu.au") == [
        "https://about.csu.edu.au/locations/campuses/bathurst/"
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.csu.edu.au", "csu.edu.au"),
        ("https://study.csu.edu.au/international/courses", "csu.edu.au"),
        ("www.herts.ac.uk", "herts.ac.uk"),
        ("https://courses.example.edu.nz", "example.edu.nz"),
    ],
)
def test_normalizes_equivalent_institution_subdomains(
    value: str,
    expected: str,
) -> None:
    assert _institution_domain(value) == expected


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


def test_allows_only_conservative_official_name_expansion() -> None:
    hostname = "www.notredame.edu.au"
    assert _can_upgrade_to_official_name(
        "Notre Dame",
        "The University of Notre Dame Australia",
        hostname,
    )
    assert not _can_upgrade_to_official_name(
        "Notre Dame",
        "Completely Unrelated University",
        hostname,
    )
    assert not _can_upgrade_to_official_name(
        "Operator Chosen University",
        "The University of Notre Dame Australia",
        hostname,
    )


def test_ai_evidence_includes_official_description_and_footer() -> None:
    page_html = """
    <html>
      <head>
        <title>Notre Dame | Notre Dame</title>
        <meta property="og:site_name" content="Notre Dame">
        <meta name="description" content="The University of Notre Dame Australia
          has Campuses in Fremantle and Broome in Western Australia, and Sydney
          in New South Wales.">
      </head>
      <body><footer>© 2026 The University of Notre Dame Australia</footer></body>
    </html>
    """
    evidence = _onboarding_ai_evidence(page_html)
    assert "The University of Notre Dame Australia" in evidence
    assert "Fremantle" in evidence
    assert "Sydney" in evidence


@pytest.mark.asyncio
async def test_openai_resolves_notre_dame_official_name_and_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_chat_json(**_kwargs):
        return {
            "website_hostname": "notredame.edu.au",
            "official_name": "The University of Notre Dame Australia",
            "country": "Australia",
            "primary_city": "Fremantle",
            "locations": [
                {
                    "display_name": "Fremantle Campus",
                    "city": "Fremantle",
                    "state_region": "Western Australia",
                    "country": "Australia",
                    "full_address": None,
                },
                {
                    "display_name": "Broome Campus",
                    "city": "Broome",
                    "state_region": "Western Australia",
                    "country": "Australia",
                    "full_address": None,
                },
                {
                    "display_name": "Sydney Campus",
                    "city": "Sydney",
                    "state_region": "New South Wales",
                    "country": "Australia",
                    "full_address": None,
                },
            ],
            "confidence": 0.98,
            "evidence_quotes": [
                "The University of Notre Dame Australia has Campuses in "
                "Fremantle and Broome in Western Australia, and Sydney in "
                "New South Wales."
            ],
        }

    monkeypatch.setattr(
        "app.services.ai.openai_client.chat_json",
        fake_chat_json,
    )
    page_html = """
    <html><head>
      <title>Notre Dame | Notre Dame</title>
      <meta name="description" content="The University of Notre Dame Australia
        has Campuses in Fremantle and Broome in Western Australia, and Sydney
        in New South Wales.">
    </head></html>
    """
    result = await _resolve_university_identity_openai(
        root_url="https://www.notredame.edu.au",
        hostname="www.notredame.edu.au",
        page_html=page_html,
        expected_country="Australia",
    )
    assert result is not None
    assert result["official_name"] == "The University of Notre Dame Australia"
    assert result["primary_city"] == "Fremantle"
    assert [location["city"] for location in result["locations"]] == [
        "Fremantle", "Broome", "Sydney",
    ]


@pytest.mark.asyncio
async def test_openai_identity_rejects_country_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_chat_json(**_kwargs):
        return {
            "website_hostname": "notredame.edu.au",
            "official_name": "University of Notre Dame",
            "country": "United States",
            "primary_city": "Notre Dame",
            "locations": [],
            "confidence": 0.99,
            "evidence_quotes": [
                "The University of Notre Dame Australia is a national university."
            ],
        }

    monkeypatch.setattr(
        "app.services.ai.openai_client.chat_json",
        fake_chat_json,
    )
    result = await _resolve_university_identity_openai(
        root_url="https://www.notredame.edu.au",
        hostname="www.notredame.edu.au",
        page_html=(
            "<meta name='description' content='The University of Notre Dame "
            "Australia is a national university.'>"
        ),
        expected_country="Australia",
    )
    assert result is None