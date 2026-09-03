from app.routers.universities import (
    _campus_page_links,
    _campus_page_location,
    _contains_encoded_html_entity,
    _decode_metadata_text,
    _extract_structured_locations,
    _metadata_title_segments,
)


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