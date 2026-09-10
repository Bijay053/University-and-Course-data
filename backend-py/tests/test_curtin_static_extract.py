from app.services.scraper.curtin_static_extract import apply_curtin_static_extraction


URL = "https://www.curtin.edu.au/study/offering/course-ug-bachelor-of-engineering-bachelor-of-commerce--bb-engcom/"


def test_combined_degree_uses_course_duration_not_credit_count():
    html = """
      <h1>Bachelor of Engineering (Honours), Bachelor of Commerce</h1>
      <div class="information">
        <div class="information__title"><h3>Duration</h3><div class="tooltip">66 credit points</div></div>
        <div class="information__content"><p>5 years, 6 months full-time</p></div>
      </div>
      <div>66 credit points</div>
      <div class="information">
        <div class="information__title"><h3>Location</h3></div>
        <div class="information__content"><p>Curtin Perth</p></div>
      </div>
      <div class="information">
        <div class="information__title"><h3>Attendance mode</h3></div>
        <div class="information__content"><p>On campus</p></div>
      </div>
      <div><strong>English language requirements</strong><span>IELTS 6.0</span></div>
      <script type="application/ld+json">
      {"@type":"Course","offers":[
        {"@type":"Offer","name":"2027 - International - Total indicative course fee (2027)","price":262086,"priceCurrency":"AUD"},
        {"@type":"Offer","name":"2027 - International - Indicative year 1 fee (2027)","price":47652,"priceCurrency":"AUD"},
        {"@type":"Offer","name":"2027 - Domestic - Indicative year 1 fee (2027)","price":8400,"priceCurrency":"AUD"}
      ]}
      </script>
    """
    result = apply_curtin_static_extraction(URL, html)
    assert result["duration"] == 5.5
    assert result["duration_term"] == "Year"
    assert result["international_fee"] == 47652.0
    assert result["fee_term"] == "Annual"
    assert result["fee_year"] == 2027
    assert result["course_location"] == "Curtin Perth"
    assert result["study_mode"] == "On Campus"
    assert result["ielts_overall"] == 6.0


def test_missing_location_is_not_defaulted_and_major_is_scoped():
    html = "<h1>Mining major</h1><div>Perth Online</div>"
    result = apply_curtin_static_extraction(
        URL.replace("bachelor-of-engineering-bachelor-of-commerce", "mining-major-bsc"),
        html,
    )
    assert result["course_location"] is None
    assert result["duration"] is None
    assert result["scrape_warnings"] == ["curtin_non_award_major"]


def test_non_curtin_urls_are_noop():
    result = apply_curtin_static_extraction("https://example.edu/course", "<b>Duration</b> 3 years")
    assert result["duration"] is None


def test_attendance_is_scoped_and_ignores_on_campus_tooltip():
    html = """
      <div class="information">
        <div class="information__title"><h3>Attendance mode</h3>
          <div class="tooltip">Not all majors are offered on campus.</div>
        </div>
        <div class="information__content"><p>Online</p></div>
      </div>
    """
    result = apply_curtin_static_extraction(URL, html)
    assert result["study_mode"] == "Online"
    assert result["course_location"] is None