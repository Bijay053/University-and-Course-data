from app.services.scraper.cdu_static_extract import (
    apply_cdu_static_extraction,
    ensure_cdu_catalogue_year,
)


def _html(*, fee: str, location: str, duration: str = "3") -> str:
    return f"""
    <html><body>
      <div class="block-course-key-fact-duration">
        <div data-student-type="domestic">
          <div>6 year/s part-time</div>
        </div>
        <div data-student-type="international">
          <div>{duration} year/s full-time</div>
        </div>
      </div>
      <div class="block-course-key-fact-location">
        <div data-student-type="domestic">Wrong campus, Online</div>
        <div data-student-type="international">{location}</div>
      </div>
      <details data-student-type="both">
        <summary id="accordion-fees">Fees</summary>
        <div data-student-type="domestic">
          Student contribution amount AUD $9,000.
        </div>
        <div data-student-type="international">
          <h4>International tuition fees</h4>
          <p>The annual tuition fee for full time study in 2026 is
             AUD ${fee}.00. This equates to $4,840 per unit.</p>
        </div>
      </details>
      <div class="related-course">
        <div data-student-type="international">Unrelated campus</div>
        <div class="block-course-key-fact-duration">
          <div data-student-type="international">0.5 year/s full-time</div>
        </div>
      </div>
      <p>Entry requires successful completion of 0.5 year of prior study.</p>
    </body></html>
    """


def test_cdu_uses_current_course_international_fee_and_location() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/bachelor-nursing-wnur02?year=2026",
        _html(fee="38,720", location="CDU Sydney, Casuarina campus"),
    )
    assert result == {
        "international_fee": 38720.0,
        "fee_term": "Annual",
        "fee_year": 2026,
        "currency": "AUD",
        "course_location": "CDU Sydney, Casuarina campus",
        "study_mode": "On Campus",
        "duration": 3.0,
        "duration_term": "Year",
    }


def test_cdu_online_is_mode_not_physical_location() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/example?year=2026",
        _html(fee="33,208", location="Casuarina campus, Online"),
    )
    assert result["course_location"] == "Casuarina campus"
    assert result["study_mode"] == "Blended"


def test_cdu_missing_international_values_fail_blank_not_domestic() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/example?year=2026",
        """
        <div class="block-course-key-fact-location">
          <div data-student-type="domestic">Casuarina campus, Online</div>
          <div data-student-type="international">Not available to international students.</div>
        </div>
        <details><summary id="accordion-fees">Fees</summary>
          <div data-student-type="domestic">AUD $9,000</div>
        </details>
        """,
    )
    assert result == {
        "international_fee": None,
        "course_location": None,
        "study_mode": None,
        "duration": None,
        "duration_term": None,
    }


def test_cdu_preserves_authoritative_one_year_graduate_entry_duration() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/bachelor-psychological-science-graduate-entry-wpsyg2?year=2026",
        _html(
            fee="33,208",
            location="Casuarina campus",
            duration="1",
        ),
    )
    assert result["duration"] == 1.0
    assert result["duration_term"] == "Year"


def test_cdu_course_url_gets_catalogue_year_without_losing_query() -> None:
    url = ensure_cdu_catalogue_year(
        "https://www.cdu.edu.au/study/course/bachelor-nursing-wnur02?source=sitemap",
        year=2026,
    )
    assert url == (
        "https://www.cdu.edu.au/study/course/bachelor-nursing-wnur02"
        "?source=sitemap&year=2026"
    )


def test_cdu_existing_catalogue_year_is_preserved() -> None:
    url = "https://www.cdu.edu.au/study/course/example?year=2025"
    assert ensure_cdu_catalogue_year(url, year=2026) == url