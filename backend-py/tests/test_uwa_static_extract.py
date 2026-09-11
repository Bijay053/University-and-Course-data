from app.services.scraper import uwa_static_extract as uwa
from app.services.scraper.uwa_static_extract import apply_uwa_static_extraction


def _page(location: str) -> str:
    return f"""
    <nav>Sydney Perth</nav>
    <div class="card-details-label">Campus location</div>
    <div class="card-details-value"><ul><li>{location}</li></ul></div>
    <div class="card-details-label">Course Code</div>
    <div class="card-details-value"><ul><li>BP026</li></ul></div>
    """


def test_uses_current_course_campus_card_not_navigation_text(monkeypatch):
    class Response:
        status_code = 200
        def json(self):
            return [{"fee": "$48,800"}]
    monkeypatch.setattr(uwa.httpx, "post", lambda *a, **k: Response())
    uwa._fee_for_code.cache_clear()
    result = apply_uwa_static_extraction(
        "https://www.uwa.edu.au/study/courses/bachelor-of-sport-and-exercise-sciences",
        _page("UWA (Crawley campus)"),
        "Bachelor of Sport and Exercise Sciences",
    )
    assert result["course_location"] == "UWA (Crawley campus)"
    assert result["location_text"] == "UWA (Crawley campus)"
    assert result["uwa_course_code"] == "BP026"
    assert result["scrape_warnings"] == []
    assert result["international_fee"] == 48800
    assert result["fee_currency"] == "AUD"
    assert result["fee_term"] == "Annual"
    assert result["fee_year"] == 2026


def test_uses_plural_locations_card_for_mba_template(monkeypatch):
    page = """
    <nav>Sydney Perth Albany</nav>
    <div class="card-details-label">Locations</div>
    <div class="card-details-value">
      <ul class="default-list"><li>Perth (Crawley campus)</li></ul>
    </div>
    <div class="card-details-label">Course Code</div>
    <div class="card-details-value"><ul><li>42520</li></ul></div>
    """
    monkeypatch.setattr(uwa, "_fee_for_code", lambda *_args: {})

    result = apply_uwa_static_extraction(
        "https://www.uwa.edu.au/study/courses/"
        "master-of-business-administration-flexible-mba",
        page,
        "Master of Business Administration (MBA) Flexible",
    )

    assert result["course_location"] == "Perth (Crawley campus)"
    assert result["location_text"] == "Perth (Crawley campus)"


def test_campus_location_label_keeps_pharmacy_campus(monkeypatch):
    page = """
    <nav>Sydney Perth Albany</nav>
    <div class="card-details-label">CAMPUS LOCATION</div>
    <div class="card-details-value">
      <ul class="default-list"><li>Perth (Crawley campus)</li></ul>
    </div>
    <div class="card-details-label">Course Code</div>
    <div class="card-details-value"><ul><li>CM039</li></ul></div>
    """
    monkeypatch.setattr(uwa, "_fee_for_code", lambda *_args: {})

    result = apply_uwa_static_extraction(
        "https://www.uwa.edu.au/study/courses/"
        "bachelor-of-human-sciences-pharmaceutical-health-and-doctor-of-pharmacy",
        page,
        "Bachelor of Human Sciences (Pharmaceutical Health) and Doctor of Pharmacy",
    )

    assert result["course_location"] == "Perth (Crawley campus)"
    assert result["location_text"] == "Perth (Crawley campus)"


def test_representative_2026_fee_values(monkeypatch):
    values = {
        "BP006": "$51,400", "BH011": "$53,700", "BH008": "$52,000",
        "BP002": "$50,700", "BP026": "$48,800",
    }
    class Response:
        status_code = 200
        def __init__(self, value): self.value = value
        def json(self): return [{"fee": self.value}]
    monkeypatch.setattr(
        uwa.httpx, "post",
        lambda _url, json, timeout: Response(values[json["courseCode"]]),
    )
    uwa._fee_for_code.cache_clear()
    for code, value in values.items():
        page = _page("Perth")
        page = page.replace("BP026", code)
        name = "Bachelor of Engineering (Honours)" if code == "BH011" else "Bachelor"
        result = apply_uwa_static_extraction("https://www.uwa.edu.au/study/courses/x", page, name)
        assert result["international_fee"] == float(value.replace("$", "").replace(",", ""))


def test_unknown_or_malformed_fee_is_fail_open(monkeypatch):
    class Response:
        status_code = 200
        def json(self): return [{"fee": "N/A"}]
    monkeypatch.setattr(uwa.httpx, "post", lambda *a, **k: Response())
    uwa._fee_for_code.cache_clear()
    result = apply_uwa_static_extraction("https://www.uwa.edu.au/study/courses/x", _page("Perth"), "Bachelor")
    assert "international_fee" not in result
    assert result["scrape_warnings"] == ["uwa_fee_calculator_required"]


def test_missing_campus_stays_missing_instead_of_defaulting_to_perth():
    result = apply_uwa_static_extraction(
        "https://www.uwa.edu.au/study/courses/bachelor-of-commerce",
        "<nav>Perth Sydney</nav>",
    )
    assert result["course_location"] is None
    assert result["location_text"] is None