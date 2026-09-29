import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors.study_mode import extract as extract_study_mode


def _config(university_id: int):
    return load_uni_config(
        slug="lsbu",
        scrape_url="https://www.lsbu.ac.uk/",
        university_id=university_id,
        name="London South Bank University",
    )


@pytest.mark.parametrize("university_id", [84, 2175])
def test_lsbu_uses_bounded_static_extraction_without_noisy_browser(university_id):
    config = _config(university_id)
    assert config.extraction.scrape_do_render is True
    assert config.extraction.skip_browser_rescue is True
    assert config.extraction.skip_per_course_browser is True
    assert config.extraction.max_parallel_fetch == 6
    assert config.extraction.study_mode.suppress_nav_rule is True
    assert config.extraction.study_mode.online_only_requires_strong_evidence is True


@pytest.mark.asyncio
async def test_lsbu_utility_online_text_is_not_course_delivery():
    set_uni_config(_config(84))
    try:
        html = """
        <main><h1>Adult Nursing BSc (Hons)</h1>
        <p>Study at Southwark Campus and Havering Campus.</p></main>
        <aside><a href="/apply">Apply online</a></aside>
        """
        evidence = await extract_study_mode(
            html, "https://www.lsbu.ac.uk/study/course-finder/adult-nursing-bsc-hons"
        )
        assert not any(item.value == "Online" for item in evidence)
    finally:
        set_uni_config(None)


@pytest.mark.asyncio
async def test_lsbu_course_owned_online_label_remains_authoritative():
    set_uni_config(_config(84))
    try:
        html = """
        <aside><a href="/apply">Apply online</a></aside>
        <main><h1>Distance Learning MSc</h1>
        <dl><dt>Mode of study</dt><dd>Online</dd></dl></main>
        """
        evidence = await extract_study_mode(
            html, "https://www.lsbu.ac.uk/study/course-finder/distance-learning-msc"
        )
        assert len(evidence) == 1
        assert evidence[0].value == "Online"
        assert evidence[0].method == "study_mode:strong_label"
    finally:
        set_uni_config(None)