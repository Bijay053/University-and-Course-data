from app.services.scraper.cdu_static_extract import apply_cdu_static_extraction


def _html(*, fee: str, location: str) -> str:
    return f"""
    <html><body>
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
      </div>
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
    }