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


def test_postgraduate_visible_international_fee_cards_supply_newest_year_one_fee():
    html = """
      <div class="fees__domestic">
        <div class="fee" data-fee-key="YR1_IND_DOM" data-fee-year="2027"
             data-segment="dom"><p>$12,000</p></div>
      </div>
      <div class="fees__international">
        <div class="fees__international--year">
          <h3>2026 International indicative fees</h3>
          <div class="fee" data-fee-key="YR1_IND_INT" data-fee-year="2026"
               data-segment="int"><h4>Indicative year 1 fee</h4><p>$39,328*</p></div>
          <div class="fee" data-fee-key="TOTAL_IND_INT" data-fee-year="2026"
               data-segment="int"><h4>Total indicative course fee</h4><p>$78,656*</p></div>
          <h3>2027 International indicative fees</h3>
          <div class="fee" data-fee-key="YR1_IND_INT" data-fee-year="2027"
               data-segment="int"><h4>Indicative year 1 fee</h4><p>$43,260*</p></div>
          <div class="fee" data-fee-key="TOTAL_IND_INT" data-fee-year="2027"
               data-segment="int"><h4>Total indicative course fee</h4><p>$86,520*</p></div>
        </div>
      </div>
    """

    result = apply_curtin_static_extraction(
        "https://www.curtin.edu.au/study/offering/"
        "course-pg-graduate-diploma-in-project-management--gd-projm/",
        html,
    )

    assert result["international_fee"] == 43260.0
    assert result["fee_currency"] == "AUD"
    assert result["fee_term"] == "Annual"
    assert result["fee_year"] == 2027


def test_research_template_supplies_current_offering_facts_and_fee():
    html = """
      <ul class="course-essentials__list">
        <li>
          <dt>Duration
            <dialog><h2>Duration</h2><p>One to two years.</p></dialog>
          </dt>
          <dd class="details-duration">2 years full-time, part-time</dd>
        </li>
        <li>
          <dt>Location
            <dialog><h2>Location</h2><p>Course teaching locations.</p></dialog>
          </dt>
          <dd><span>Curtin Perth</span></dd>
        </li>
      </ul>
      <div class="course-locations">
        <div class="locations__period">
          <h6>Research Term 1</h6><p>On campus</p>
        </div>
        <div class="locations__period">
          <h6>Research Term 2</h6><p>Online</p>
        </div>
      </div>
      <section class="fees-and-charges">
        <div class="fees-charges__box purple">
          <div class="fees-charges__item fees-charges__item--int">
            <h4 class="fees-charges__fee-title">
              Indicative year 1 fee (2026)
            </h4>
            <p class="fees-charges__fee h3">$38,220*</p>
          </div>
          <div class="fees-charges__item fees-charges__item--int">
            <h4 class="fees-charges__fee-title">
              Total indicative course fee (2027)
            </h4>
            <p class="fees-charges__fee h3">$80,262*</p>
          </div>
          <div class="fees-charges__item fees-charges__item--int">
            <h4 class="fees-charges__fee-title">
              Indicative year 1 fee (2027)
            </h4>
            <p class="fees-charges__fee h3">$40,131*</p>
          </div>
        </div>
        <div class="fees-charges__box">
          <div class="fees-charges__item">
            <h4 class="fees-charges__fee-title">
              Indicative year 1 fee (2028)
            </h4>
            <p class="fees-charges__fee h3">$12,000*</p>
          </div>
        </div>
      </section>
    """

    result = apply_curtin_static_extraction(
        "https://www.curtin.edu.au/study/offering/"
        "course-research-master-of-philosophy-information-systems--mr-isys/",
        html,
    )

    assert result["international_fee"] == 40131.0
    assert result["fee_currency"] == "AUD"
    assert result["fee_term"] == "Annual"
    assert result["fee_year"] == 2027
    assert result["duration"] == 2.0
    assert result["duration_term"] == "Year"
    assert result["course_location"] == "Curtin Perth"
    assert result["study_mode"] == "Blended"


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