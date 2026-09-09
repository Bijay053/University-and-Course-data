"""Global enforcement tests for domestic-only and online-only courses."""
from __future__ import annotations

import pytest

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.guards import (
    is_confirmed_host_online_only_page,
    should_stage_course,
)
from app.services.scraper.pipelines.single_course import (
    _domestic_only_filter_enabled,
    _duration_labeled_values,
    _infer_study_load_from_text,
    _is_adelaide_online_only_page,
    _is_domestic_only_page,
    _is_parttime_only_page,
    _parttime_only_filter_enabled,
)


def test_disabled_legacy_overrides_cannot_bypass_global_delivery_filters() -> None:
    """A stale YAML/admin `enabled: false` must not admit ineligible courses."""
    config = UniConfig.model_validate(
        {
            "slug": "legacy-disabled-example",
            "name": "Legacy Disabled Example University",
            "base_url": "https://example.edu",
            "scrape_url": "https://example.edu/courses",
            "extraction": {
                "filters": {
                    "domestic_only": {"enabled": False},
                    "online_only": {"enabled": False},
                }
            }
        }
    )
    token = current_uni_config.set(config)
    try:
        assert _domestic_only_filter_enabled() is True

        online_ok, online_reason = should_stage_course(
            "Online Master of Business",
            {
                "course_name": "Online Master of Business",
                "international_fee": 30000,
                "study_mode": "Online",
            },
            source_url="https://example.edu/courses/master-of-business-online",
        )
        assert online_ok is False
        assert online_reason == "online_only"

        domestic_ok, domestic_reason = should_stage_course(
            "Master of Business",
            {
                "course_name": "Master of Business",
                "international_fee": 30000,
                "study_mode": "Blended",
                "domestic_only": True,
            },
            source_url="https://example.edu/courses/master-of-business",
        )
        assert domestic_ok is False
        assert domestic_reason == "domestic_only"
    finally:
        current_uni_config.reset(token)


def test_scu_hidden_audience_selector_marks_domestic_only_course() -> None:
    html = """
    <form class="course-filter">
      <div style="display:none">
        <label for="course-location">Show me course information for</label>
        <select id="course-location">
          <option value="default">Domestic</option>
          <option value="international">International</option>
        </select>
      </div>
    </form>
    <div data-course="international">
      <h3>International snapshot</h3>
      <p>5 years full-time</p>
    </div>
    """
    assert _is_domestic_only_page(
        html,
        "https://www.scu.edu.au/study/courses/domestic-only/2027/",
    )


def test_scu_visible_audience_selector_keeps_international_course() -> None:
    html = """
    <form class="course-filter">
      <div>
        <label for="course-location">Show me course information for</label>
        <select id="course-location">
          <option value="default">Domestic</option>
          <option value="international">International</option>
        </select>
      </div>
    </form>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.scu.edu.au/study/courses/international/2027/",
    )


def test_adelaide_dormant_domestic_modal_does_not_reject_international_degree() -> None:
    html = """
    <meta property="studentType" content="Domestic|International"/>
    <div id="audience-switcher-dom-int-content">
      <select id="int-modal">
        <option value="Domestic">Australian student</option>
        <option value="International">International student</option>
      </select>
    </div>
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    <section class="degree-details">
      <p>Published fees are for international students starting in 2026.</p>
      <p>International tuition fee: $43,400</p>
      <p>CRICOS 097508M</p>
    </section>
    """
    assert not _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/bachelor-of-arts/",
    )


def test_adelaide_exclusive_audience_switch_rejects_metadata_inconsistent_degree() -> None:
    html = """
    <meta property="studentType" content="Domestic|International"/>
    <div id="audience-switcher-exclusively-dom-int-content">
      <select id="int-modal-exclusive">
        <option value="Domestic">Australian student</option>
        <option value="International">International student</option>
      </select>
    </div>
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    """
    assert _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/diploma-in-building-studies/",
    )


def test_adelaide_open_domestic_dialog_rejects_rendered_recovery_page() -> None:
    html = """
    <meta property="studentType" content="Domestic|International"/>
    <dialog
      open=""
      data-modal-opener="dom-modal-exclusive"
      role="dialog"
      aria-modal="true"
    >
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    """
    assert _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/diploma-in-building-studies/",
    )


@pytest.mark.asyncio
async def test_adelaide_diploma_pipeline_exits_as_domestic_only() -> None:
    config = UniConfig.model_validate(
        {
            "slug": "adelaide",
            "name": "Adelaide University",
            "base_url": "https://adelaide.edu.au",
            "scrape_url": "https://adelaide.edu.au/study/degrees/",
            "extraction": {
                "filters": {
                    "domestic_only": {"enabled": False},
                }
            },
        }
    )
    html = """
    <html>
      <head>
        <meta property="studentType" content="Domestic|International"/>
      </head>
      <body>
        <h1>Diploma in Building Studies</h1>
        <div id="audience-switcher-exclusively-dom-int-content">
          <select id="int-modal-exclusive">
            <option value="Domestic">Australian student</option>
            <option value="International">International student</option>
          </select>
        </div>
        <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
          <h2>This degree is only available to Australian students</h2>
        </dialog>
      </body>
    </html>
    """
    token = current_uni_config.set(config)
    try:
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://adelaide.edu.au/study/degrees/diploma-in-building-studies/",
            html=html,
            use_ai_fallback=False,
        )
    finally:
        current_uni_config.reset(token)

    assert result["payload"]["domestic_only"] is True


def test_adelaide_international_only_exclusive_switch_remains_eligible() -> None:
    html = """
    <meta property="studentType" content="International"/>
    <div id="audience-switcher-exclusively-dom-int-content">
      <select id="int-modal-exclusive">
        <option value="Domestic">Australian student</option>
        <option value="International">International student</option>
      </select>
    </div>
    <dialog
      open=""
      data-modal-opener="dom-modal-exclusive"
      role="dialog"
    >
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    """
    assert not _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/"
        "international-master-of-business-administration/",
    )


def test_adelaide_online_catalogue_route_is_authoritative() -> None:
    html = """
    <nav>
      <a href="/study/online/">100% online study</a>
      <a href="/life-at-adelaide/campuses/">Campuses</a>
    </nav>
    """
    assert _is_adelaide_online_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/online/"
        "bachelor-of-business-economics-finance-and-trade/"
        "#section-entry-requirements",
    )


def test_adelaide_paired_online_metadata_rejects_noncanonical_alias() -> None:
    html = """
    <meta property="courseMode" content="100% online"/>
    <meta property="location" content="Online"/>
    """
    assert _is_adelaide_online_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/"
        "bachelor-of-business-economics-finance-and-trade/",
    )


def test_adelaide_shared_online_navigation_does_not_reject_campus_degree() -> None:
    html = """
    <meta property="courseMode" content="On campus"/>
    <meta property="location" content="Adelaide City Campus"/>
    <nav>
      <a href="/study/online/">100% online study</a>
      <a href="/life-at-adelaide/campuses/">Campuses</a>
    </nav>
    """
    assert not _is_adelaide_online_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/bachelor-of-arts/",
    )


@pytest.mark.parametrize(
    ("url", "html"),
    [
        (
            "https://www.herts.ac.uk/courses/undergraduate/"
            "bsc-hons-information-technology-online",
            """
            <main>
              <h1>BSc (Hons) Information Technology (Online)</h1>
              <p>Our online degrees let you study when and where you want.</p>
            </main>
            """,
        ),
        (
            "https://www.westminster.ac.uk/health-psychology-courses/2026-27/"
            "september/open-distance-learning-full-time/"
            "digital-health-and-cyberpsychology-msc",
            """
            <main>
              <h1>Digital Health and Cyberpsychology MSc</h1>
              <dl>
                <dt>Attendance</dt><dd>Open/Distance Learning</dd>
                <dt>Campus</dt><dd>Distance Learning Online</dd>
              </dl>
            </main>
            """,
        ),
        (
            "https://www.tees.ac.uk/postgraduate_courses/"
            "english_&_creative_writing/ma_creative_writing_(online).cfm",
            """
            <main>
              <h1>Creative Writing (Online) MA</h1>
              <div>100% online</div>
              <p>There is no requirement to attend classes.</p>
            </main>
            """,
        ),
        (
            "https://www.manchester.ac.uk/study/masters/courses/list/21020/"
            "msc-pollution-and-environmental-control-online/",
            """
            <main>
              <h1>Pollution and Environmental Control (online)</h1>
              <dl><dt>Delivery</dt><dd>100% online learning</dd></dl>
            </main>
            """,
        ),
        (
            "https://www.qmul.ac.uk/postgraduate/taught/coursefinder/courses/"
            "technology-media-and-telecommunications-law-online-pgdip/",
            """
            <main>
              <h1>Technology, Media and Telecommunications Law Online PGDip</h1>
              <dl>
                <dt>Location</dt><dd>Distance Learning</dd>
                <dt>Fees</dt><dd>Home/Overseas: £11,150</dd>
              </dl>
            </main>
            """,
        ),
        (
            "https://www.kingston.ac.uk/study/postgraduate/"
            "psychology-msc-conversion-online",
            """
            <main>
              <h1>Psychology MSc (Conversion) Online</h1>
              <p>Study psychology while studying fully online.</p>
              <p>Teaching is delivered entirely online.</p>
            </main>
            """,
        ),
    ],
)
def test_audited_host_templates_reject_online_only_courses(
    url: str,
    html: str,
) -> None:
    assert is_confirmed_host_online_only_page(html, url)


@pytest.mark.parametrize(
    ("url", "html"),
    [
        (
            "https://courses.uwe.ac.uk/KN2B6/"
            "real-estate-finance-and-investment-distance-learning",
            """
            <main>
              <h1>MSc Real Estate Finance and Investment (Distance learning)</h1>
              <p>You are mainly taught online.</p>
              <p>You must attend a one-week block at Frenchay Campus.</p>
            </main>
            """,
        ),
        (
            "https://www.flinders.edu.au/study/courses/"
            "bachelor-social-work-external",
            """
            <main>
              <h1>Bachelor of Social Work</h1>
              <dl><dt>Delivery mode</dt><dd>In person and Online</dd></dl>
              <p>Students attend 20 days at Bedford Park in intensive blocks.</p>
            </main>
            """,
        ),
        (
            "https://www.sruc.ac.uk/course-catalogue/"
            "wildlife-and-conservation-management/"
            "postgraduate-certificate-wildlife-and-conservation-management-"
            "distance-learning/",
            """
            <main>
              <h1>Postgraduate Certificate Wildlife and Conservation Management</h1>
              <p>Study mode: Distance Learning.</p>
              <p>This programme requires two in-person study weekends.</p>
            </main>
            """,
        ),
        (
            "https://www.uel.ac.uk/postgraduate/courses/"
            "mphil-phd-health-wellbeing-online-harms",
            """
            <main>
              <h1>Health, Wellbeing and Online Harms MPhil PhD</h1>
              <p>Study on campus in London.</p>
            </main>
            """,
        ),
        (
            "https://www.unitec.ac.nz/current-students/on-campus/"
            "external-support-services/",
            """
            <main>
              <h1>External Support Services</h1>
              <p>Support information for students studying on campus.</p>
            </main>
            """,
        ),
    ],
)
def test_audited_online_parent_rules_keep_mixed_and_false_positive_pages(
    url: str,
    html: str,
) -> None:
    assert not is_confirmed_host_online_only_page(html, url)


def test_audited_host_rule_requires_course_owned_delivery_evidence() -> None:
    html = """
    <main>
      <h1>Digital Health and Cyberpsychology MSc</h1>
      <p>Attendance: On campus</p>
    </main>
    <nav><a href="/study/online/">Explore online study</a></nav>
    """
    assert not is_confirmed_host_online_only_page(
        html,
        "https://www.westminster.ac.uk/health-psychology-courses/2026-27/"
        "september/open-distance-learning-full-time/"
        "digital-health-and-cyberpsychology-msc",
    )


def test_optional_campus_event_does_not_override_online_only_delivery() -> None:
    html = """
    <main>
      <h1>Digital Health and Cyberpsychology MSc</h1>
      <dl>
        <dt>Attendance</dt><dd>Open/Distance Learning</dd>
        <dt>Campus</dt><dd>Distance Learning Online</dd>
      </dl>
      <p>Online students may attend an optional graduation event on campus.</p>
    </main>
    """
    assert is_confirmed_host_online_only_page(
        html,
        "https://www.westminster.ac.uk/health-psychology-courses/2026-27/"
        "september/open-distance-learning-full-time/"
        "digital-health-and-cyberpsychology-msc",
    )


@pytest.mark.parametrize(
    ("url", "html"),
    [
        (
            "https://www.herts.ac.uk/courses/undergraduate/"
            "mixed-computing-online",
            """
            <main>
              <h1>Mixed Computing (Online)</h1>
              <p>Students must attend weekly classes at Hatfield Campus.</p>
              <template><p>Our online degrees can be studied anywhere.</p></template>
            </main>
            """,
        ),
        (
            "https://www.westminster.ac.uk/health-psychology-courses/2026-27/"
            "september/open-distance-learning-full-time/mixed-health-msc",
            """
            <main>
              <h1>Mixed Health MSc</h1>
              <p>Attendance: On campus</p>
            </main>
            <footer>
              <p>Attendance Open/Distance Learning</p>
              <p>Campus Distance Learning Online</p>
            </footer>
            """,
        ),
        (
            "https://www.tees.ac.uk/postgraduate_courses/business/"
            "msc_mixed_business_(online).cfm",
            """
            <div id="coursepage">
              <h1>Mixed Business (Online) MSc</h1>
              <p>Students must attend classes on campus.</p>
              <div hidden>100% online</div>
            </div>
            """,
        ),
        (
            "https://www.manchester.ac.uk/study/masters/courses/list/99999/"
            "msc-mixed-science-online/",
            """
            <main id="content">
              <h1>Mixed Science (online)</h1>
              <p>Students must attend practical classes in Manchester.</p>
              <div aria-hidden="true">Delivery: 100% online learning</div>
            </main>
            """,
        ),
        (
            "https://www.qmul.ac.uk/postgraduate/taught/coursefinder/courses/"
            "mixed-law-online-pgdip/",
            """
            <main>
              <h1>Mixed Law Online PGDip</h1>
              <dl><dt>Location</dt><dd>Lincoln's Inn Fields</dd></dl>
            </main>
            <footer><p>Location Distance Learning</p></footer>
            """,
        ),
        (
            "https://www.kingston.ac.uk/study/postgraduate/"
            "mixed-psychology-online",
            """
            <main>
              <h1>Mixed Psychology Online</h1>
              <p>Students must attend practical sessions on campus.</p>
              <template><p>Teaching is delivered entirely online.</p></template>
            </main>
            """,
        ),
    ],
)
def test_audited_host_templates_ignore_hidden_chrome_and_keep_mixed_courses(
    url: str,
    html: str,
) -> None:
    assert not is_confirmed_host_online_only_page(html, url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "course_name", "html"),
    [
        (
            "https://www.herts.ac.uk/courses/undergraduate/"
            "bsc-hons-information-technology-online",
            "BSc (Hons) Information Technology (Online)",
            """
            <main>
              <h1>BSc (Hons) Information Technology (Online)</h1>
              <p>Our online degrees let you study when and where you want.</p>
            </main>
            """,
        ),
        (
            "https://www.westminster.ac.uk/health-psychology-courses/2026-27/"
            "september/open-distance-learning-full-time/"
            "digital-health-and-cyberpsychology-msc",
            "Digital Health and Cyberpsychology MSc",
            """
            <main>
              <h1>Digital Health and Cyberpsychology MSc</h1>
              <dl>
                <dt>Attendance</dt><dd>Open/Distance Learning</dd>
                <dt>Campus</dt><dd>Distance Learning Online</dd>
              </dl>
            </main>
            """,
        ),
        (
            "https://www.tees.ac.uk/postgraduate_courses/"
            "english_&_creative_writing/ma_creative_writing_(online).cfm",
            "Creative Writing (Online) MA",
            """
            <main>
              <h1>Creative Writing (Online) MA</h1>
              <p>Attendance: 100% online</p>
            </main>
            """,
        ),
        (
            "https://www.manchester.ac.uk/study/masters/courses/list/21020/"
            "msc-pollution-and-environmental-control-online/",
            "Pollution and Environmental Control (online) MSc",
            """
            <main>
              <h1>Pollution and Environmental Control (online)</h1>
              <dl><dt>Delivery</dt><dd>100% online learning</dd></dl>
            </main>
            """,
        ),
        (
            "https://www.qmul.ac.uk/postgraduate/taught/coursefinder/courses/"
            "technology-media-and-telecommunications-law-online-pgdip/",
            "Technology, Media and Telecommunications Law Online PGDip",
            """
            <main>
              <h1>Technology, Media and Telecommunications Law Online PGDip</h1>
              <dl><dt>Location</dt><dd>Distance Learning</dd></dl>
            </main>
            """,
        ),
        (
            "https://www.kingston.ac.uk/study/postgraduate/"
            "psychology-msc-conversion-online",
            "Psychology MSc (Conversion) Online",
            """
            <main>
              <h1>Psychology MSc (Conversion) Online</h1>
              <p>Teaching is delivered entirely online.</p>
            </main>
            """,
        ),
    ],
)
async def test_audited_host_pipeline_exits_as_online_only(
    url: str,
    course_name: str,
    html: str,
) -> None:
    config = UniConfig.model_validate(
        {
            "slug": "audited-online-template",
            "name": "Audited Online Template",
            "base_url": f"https://{url.split('/')[2]}",
            "scrape_url": f"https://{url.split('/')[2]}/",
        }
    )
    token = current_uni_config.set(config)
    try:
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            url,
            html=html,
            use_ai_fallback=False,
        )
    finally:
        current_uni_config.reset(token)

    assert result["payload"]["online_only"] is True
    assert result["payload"]["online_only_audited_host"] is True
    accepted, reason = should_stage_course(
        course_name,
        result["payload"],
        source_url=result["url"],
    )
    assert accepted is False
    assert reason == "online_only"


@pytest.mark.asyncio
async def test_adelaide_online_degree_pipeline_exits_before_mode_overwrite() -> None:
    config = UniConfig.model_validate(
        {
            "slug": "adelaide",
            "name": "Adelaide University",
            "base_url": "https://adelaide.edu.au",
            "scrape_url": "https://adelaide.edu.au/study/degrees/",
            "extraction": {
                "filters": {
                    "online_only": {"enabled": False},
                }
            },
        }
    )
    html = """
    <html>
      <head>
        <meta property="studentType" content="Domestic|International"/>
        <meta property="courseMode" content="100% online"/>
        <meta property="location" content="Online"/>
      </head>
      <body>
        <h1>Bachelor of Business (Economics, Finance and Trade)</h1>
        <span class="cmp-herobanner__pretitle">100% online</span>
        <nav>
          <a href="/life-at-adelaide/campuses/adelaide-city-campus/">
            Adelaide City Campus
          </a>
        </nav>
      </body>
    </html>
    """
    token = current_uni_config.set(config)
    try:
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://adelaide.edu.au/study/degrees/online/"
            "bachelor-of-business-economics-finance-and-trade/",
            html=html,
            use_ai_fallback=False,
        )
    finally:
        current_uni_config.reset(token)

    assert result["payload"]["online_only"] is True
    assert result["payload"]["online_only_adelaide"] is True
    accepted, reason = should_stage_course(
        "Bachelor of Business (Economics, Finance and Trade)",
        result["payload"],
        source_url=result["url"],
    )
    assert accepted is False
    assert reason == "online_only"


def test_adelaide_domestic_student_type_metadata_rejects_course() -> None:
    html = """
    <meta property="studentType" content="Domestic"/>
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    <main>
      <p>Indicative annual fee: $25,000</p>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/"
        "graduate-certificate-in-oral-health-science/",
    )


def test_adelaide_student_type_metadata_is_attribute_order_independent() -> None:
    html = '<meta content="Domestic" data-source="degree" property="studentType"/>'
    assert _is_domestic_only_page(
        html,
        "https://www.adelaide.edu.au/study/degrees/domestic-program/",
    )


def test_adelaide_real_course_level_domestic_statement_still_rejects() -> None:
    html = """
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    <main>
      <p>This degree is only available to domestic applicants.</p>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/domestic-program/",
    )


def test_utas_soft_international_caveat_is_not_course_level_rejection() -> None:
    """Rendered/compacted UTAS HTML may retain the caveat but omit hidden tabs."""
    html = """
    <main>
      <h1>Bachelor of Information and Communication Technology</h1>
      <p>This course may not be available to international students.</p>
      <p>Study on campus in Hobart or Launceston.</p>
    </main>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/tsbe/courses/p3t-bachelor-of-ict",
    )


def test_utas_hard_domestic_only_statement_still_rejects() -> None:
    html = """
    <main>
      <h1>Domestic Program</h1>
      <p>This course is only available to domestic students.</p>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/example/courses/domestic-program",
    )


def test_utas_shared_distance_disclaimer_does_not_reject_on_campus_course() -> None:
    html = """
    <main>
      <p>This course may not be available to international students.</p>
      <p>Please <a href="/distance">see the list of distance courses</a>
      for available options.</p>
      <p>Study on campus in Hobart or Launceston.</p>
    </main>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/example/courses/distance-program",
    )


def test_utas_advisory_only_international_panel_rejects_domestic_course() -> None:
    html = """
    <main>
      <h1>Bachelor of Outdoor and Environmental Education</h1>
      <div id="tabDomestic">
        <p>Commonwealth Supported places available</p>
      </div>
      <div id="tabInternational" hidden>
        This course may not be available to international students.
        Please see the <a href="/study/online">list of distance courses</a>
        (i.e. online and taken outside Australia) that are offered to
        international students
      </div>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/arts-soc/courses/"
        "a3f-bachelor-of-outdoor-and-environmental-education?year=2026",
    )


def test_utas_substantive_international_panel_keeps_eligible_course() -> None:
    html = """
    <main>
      <p>This course may not be available to international students.</p>
      <p>Please see the list of distance courses that are offered to
      international students.</p>
      <div id="tabInternational" hidden>
        <h2>Key Information</h2>
        <p>CRICOS: 002346B</p>
        <p>Duration: Minimum 3 years</p>
        <p>Location: Hobart — Semester 1, Semester 2</p>
      </div>
    </main>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/tsbe/courses/"
        "b3a-bachelor-of-business?year=2026",
    )


def test_utas_explicit_full_time_option_beats_shared_part_time_prose() -> None:
    html = """
    <section>
      <div>Duration</div>
      <div>
        Minimum 3 years, up to a maximum of 7 years.
        This course is available to study as both part-time or full-time.
      </div>
      <div>Duration</div>
      <div>
        Duration refers to the minimum and maximum amounts of time in which
        this course can be completed. Some programs are only available part time.
      </div>
    </section>
    """
    assert _is_parttime_only_page(html) is False


def test_utas_shared_part_time_prose_alone_is_not_course_level_evidence() -> None:
    html = """
    <section>
      <div>Duration</div>
      <div>
        Minimum 3 years, up to a maximum of 7 years.
        Duration refers to the minimum and maximum amounts of time in which
        this course can be completed. It will be affected by whether you choose
        to study full or part time, noting that some programs are only available
        part time.
      </div>
    </section>
    """
    assert _is_parttime_only_page(html) is False


def test_full_time_wins_when_duration_also_mentions_part_time_equivalent() -> None:
    assert (
        _infer_study_load_from_text("2 years full-time or part-time equivalent")
        == "Full Time"
    )
    assert (
        _infer_study_load_from_text("3 years, or part-time equivalent")
        == "Full Time"
    )
    assert (
        _infer_study_load_from_text("3 years (or part-time equivalent)")
        == "Full Time"
    )


def test_unisq_parenthesised_part_time_equivalent_is_not_part_time_only() -> None:
    html = """
    <ul class="details-listing">
      <li>
        <span class="details-listing__title">Duration</span>
        <span class="details-listing__value">
          3 years (or part-time equivalent)
        </span>
      </li>
    </ul>
    """
    assert _duration_labeled_values(html) == [
        "3 years (or part-time equivalent)"
    ]
    assert _is_parttime_only_page(html) is False


def test_explicit_part_time_only_wording_overrides_equivalent_full_time_measure() -> None:
    assert (
        _infer_study_load_from_text(
            "Duration 1 year equivalent full-time study. Only available part-time."
        )
        == "Part Time"
    )


def test_uow_duration_row_is_not_part_time_only_when_full_time_is_offered() -> None:
    html = """
    <div class="cf-college-info__row">
      <div class="cf-college-info__left"><span>Duration</span></div>
      <div class="cf-college-info__right">
        2 years full-time or part-time equivalent
      </div>
    </div>
    """
    assert _is_parttime_only_page(html) is False


def test_nested_duration_label_reads_its_list_item_value() -> None:
    html = """
    <ul class="details-listing">
      <li>
        <span class="details-listing__title"><strong>Duration</strong></span>
        <span class="details-listing__value">
          3 years full-time or equivalent part-time
        </span>
      </li>
    </ul>
    """
    assert (
        _infer_study_load_from_text(" ".join(_duration_labeled_values(html)))
        == "Full Time"
    )


def test_part_time_only_duration_is_globally_rejected() -> None:
    html = """
    <div class="cf-college-info__row">
      <div class="cf-college-info__left"><span>Duration</span></div>
      <div class="cf-college-info__right">2 years part-time</div>
    </div>
    """
    assert _parttime_only_filter_enabled() is True
    assert _is_parttime_only_page(html) is True

    accepted, reason = should_stage_course(
        "Master of Part-Time Study",
        {
            "course_name": "Master of Part-Time Study",
            "international_fee": 30000,
            "study_mode": "On Campus",
            "study_load": "Part Time",
        },
        source_url="https://example.edu/master-of-part-time-study",
    )
    assert accepted is False
    assert reason == "part_time_only"