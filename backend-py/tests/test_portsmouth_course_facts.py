import json

import pytest

from app.services.scraper.extractors import fee, study_mode


URL = "https://www.port.ac.uk/study/courses/postgraduate-taught/msc-accounting"


def page(modes, body=""):
    data = {
        "@graph": [
            {
                "@type": "Course",
                "url": URL,
                "hasCourseInstance": [
                    {"@type": "CourseInstance", "courseMode": mode}
                    for mode in modes
                ],
            },
            {
                "@type": "Course",
                "url": URL + "-related",
                "hasCourseInstance": [{"courseMode": ["Online"]}],
            },
        ],
    }
    return (
        '<script type="application/ld+json">'
        + json.dumps(data)
        + "</script>"
        + body
    )


@pytest.mark.asyncio
async def test_port_course_instance_outweighs_negated_distance_copy_and_navigation():
    html = page(
        [["Full-time", "On campus"]],
        "<nav>Online courses</nav>Not taught by Distance Learning",
    )
    result = await study_mode.extract(html, URL)
    assert [(row.value, row.method) for row in result] == [
        ("On Campus", "study_mode:port_course_instance")
    ]


@pytest.mark.asyncio
async def test_port_course_instance_keeps_online_only_and_mixed_offerings():
    online = await study_mode.extract(page([["Online"]]), URL)
    mixed = await study_mode.extract(
        page([["Online"], ["Full-time", "On campus"]]), URL
    )
    assert online[0].value == "Online"
    assert mixed[0].value == "Blended"
    assert study_mode._port_course_instance_mode(
        page([["On campus"]]), URL + "-other"
    ) is None
    assert study_mode.port_course_instance_modes(
        page([["Part-time", "On campus"]]), URL
    ) == ["Part-time", "On campus"]


@pytest.mark.asyncio
async def test_port_fee_prefers_international_full_time_over_uk_and_part_time():
    html = page([], """
    <div class="accordion-item"><span class="accordion-item__button-title">
    UK, Channel Islands, and Isle of Man students</span>Full-time: £11,850</div>
    <div class="accordion-item"><span class="accordion-item__button-title">
    International and EU students</span>Full-time: £18,600
    Part-time: £6,200 per year</div>
    <div class="accordion-item"><span class="accordion-item__button-title">
    International and EU students</span>2027 fees to be confirmed</div>
    """)
    rows = await fee.extract(html, URL, country="United Kingdom")
    assert len(rows) == 1
    assert rows[0].value == 18600
    assert rows[0].normalized["fee_term"] == "Annual"


@pytest.mark.asyncio
async def test_port_fee_does_not_promote_domestic_only_hnc_or_part_time():
    uk_only = page([], """
    Tuition fees Close all September 2026 start UK, Channel Islands, and
    Isle of Man students Part time: £4,240 per year
    """)
    assert fee.portsmouth_international_fee(uk_only, URL) == (True, None)
    assert await fee.extract(uk_only, URL, country="United Kingdom") == []
    part_time = page([], """
    <div class="accordion-item"><span class="accordion-item__button-title">
    International and EU students</span>Part-time: £6,200 per year</div>
    """)
    assert fee.portsmouth_international_fee(part_time, URL) == (True, None)
    assert await fee.extract(part_time, URL, country="United Kingdom") == []


@pytest.mark.asyncio
async def test_port_specialist_short_course_can_have_low_explicit_intl_fee():
    html = page([], """
    <div class="accordion-item"><span class="accordion-item__button-title">
    International and EU students</span>£3,230 (may be subject to increase)</div>
    """)
    rows = await fee.extract(html, URL, country="United Kingdom")
    assert rows[0].value == 3230
    assert rows[0].normalized["fee_term"] == "Full Course"