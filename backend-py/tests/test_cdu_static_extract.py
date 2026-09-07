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


def test_cdu_vet_uses_student_visa_fee_and_headline_full_time_duration() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/"
        "sit50422-diploma-hospitality-management-sit50422?year=2026",
        """
        <div class="block-course-key-fact-duration-vet">
          <div>
            <h3>Duration</h3>
            <div>2 year/s</div>
            <div data-student-type="domestic">
              This program is delivered over a period of 6 months to 1 year.
            </div>
            <div data-student-type="international">
              Student Visa holders must study internally full time
            </div>
          </div>
        </div>
        <details>
          <summary id="accordion-fees">Fees</summary>
          <div data-student-type="domestic">
            <table><tr><td>Full Fee</td><td>$13,917.45</td></tr></table>
            International non-student visa-holders; fees may vary by visa type.
          </div>
          <div data-student-type="international">
            <h4>International tuition Fees</h4>
            <p>The annual tuition fee for commencing student visa holders in
               2026 is AUD $17,420.00.</p>
          </div>
        </details>
        """,
    )
    assert result["international_fee"] == 17420.0
    assert result["fee_term"] == "Annual"
    assert result["fee_year"] == 2026
    assert result["currency"] == "AUD"
    assert result["duration"] == 2.0
    assert result["duration_term"] == "Year"


def test_cdu_vet_does_not_use_headline_duration_without_student_visa_route() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/domestic-vet-example?year=2026",
        """
        <div class="block-course-key-fact-duration-vet">
          <div>
            <h3>Duration</h3>
            <div>1.5 year/s</div>
            <div data-student-type="domestic">1.5 years full-time</div>
          </div>
        </div>
        """,
    )
    assert result["duration"] is None
    assert result["duration_term"] is None


def test_cdu_vet_ignores_preceding_related_course_duration() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/"
        "sit40521-certificate-iv-kitchen-management-sit40521?year=2026",
        """
        <div class="related-course">
          <div class="block-course-key-fact-duration-vet">
            <h3>Duration</h3>
            <div>0.5 year/s</div>
            <div data-student-type="international">
              Student Visa holders must study internally full time
            </div>
          </div>
        </div>
        <section id="key-details">
          <div class="block-course-key-fact-duration-vet">
            <h3>Duration</h3>
            <div>1.5 year/s</div>
            <div data-student-type="international">
              Student Visa holders must study internally full time
            </div>
          </div>
        </section>
        """,
    )
    assert result["duration"] == 1.5
    assert result["duration_term"] == "Year"


def test_cdu_uses_course_specific_english_overalls_not_component_floors() -> None:
    result = apply_cdu_static_extraction(
        "https://www.cdu.edu.au/study/course/doctor-pharmacy-spha01?year=2026",
        """
        <h2>English Language requirements</h2>
        <table>
          <tr>
            <td>IELTS Academic Module (including One Skill Retake)</td>
            <td>A minimum overall score of 6.5 with no band less than 6.0.</td>
          </tr>
          <tr>
            <td>Cambridge Advanced English (CAE)</td>
            <td>A minimum overall score of 176, with no skill below 169.</td>
          </tr>
          <tr>
            <td>Pearson Test of English (PTE) Academic module</td>
            <td>A minimum overall score of 58 with no score lower than 50.</td>
          </tr>
          <tr>
            <td>TOEFL Internet-based Test (iBT)</td>
            <td>A minimum overall score of 79 with a minimum writing score of 21.</td>
          </tr>
        </table>
        """,
    )

    assert result["ielts_overall"] == 6.5
    assert result["ielts_listening"] == 6.0
    assert result["ielts_reading"] == 6.0
    assert result["ielts_writing"] == 6.0
    assert result["ielts_speaking"] == 6.0
    assert result["pte_overall"] == 58
    assert result["pte_listening"] == 50
    assert result["pte_reading"] == 50
    assert result["pte_writing"] == 50
    assert result["pte_speaking"] == 50
    assert result["toefl_overall"] == 79
    assert result["toefl_writing"] == 21
    assert result["cambridge_overall"] == 176


def test_cdu_direct_english_table_cannot_be_overwritten_by_generic_sources() -> None:
    from app.services.scraper.pipelines.single_course import can_override

    assert not can_override("cdu_static", "regex")
    assert not can_override("cdu_static", "central_page:english")
    assert not can_override("cdu_static", "gemini_primary")


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