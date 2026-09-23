import pytest

from app.services.scraper import central_pages
from app.services.scraper.central_pages import (
    _parse_column_keyed_english_table,
    _parse_program_keyed_english_tables,
)
from app.services.scraper.config.loader import get_config_for_host
def test_mdx_recipe_uses_current_catalogue_sitemap() -> None:
    cfg = get_config_for_host(
        hostname="www.mdx.ac.uk",
        name="Middlesex University",
        scrape_url="https://www.mdx.ac.uk",
        university_id=83,
        create_missing_stub=False,
    )

    assert cfg.discovery.sitemap_url == "https://www.mdx.ac.uk/sitemap.xml"
    assert cfg.discovery.always_sitemap_supplement is False
    assert cfg.discovery.allow_url_patterns == [
        r"/courses/(?:undergraduate|postgraduate)/[^/?#]+/?$"
    ]


from app.services.scraper.pipelines.single_course import (
    _select_central_english_program,
)


MDX_ENGLISH_TABLE = """
<table>
  <tr>
    <th>Qualification</th>
    <th>Undergraduate programmes (apart from exceptional programmes)</th>
    <th>Business and Law postgraduate programmes</th>
    <th>Postgraduate courses (apart from exceptional programmes)</th>
    <th><a href="#mdx-exceptional-courses">Exceptions</a></th>
  </tr>
  <tr>
    <td>IELTS (both online and paper-based exams accepted)</td>
    <td>6.0 (with 5.5 in each component)</td>
    <td>6.0 (with 6 in reading/writing, min 5.5 in other components)</td>
    <td>6.5 (with 6.0 in each component)</td>
    <td>7.0</td>
  </tr>
</table>
<span id="mdx-exceptional-courses"></span>
<h2>Exceptional courses</h2>
<ul>
  <li>Advanced Clinical Practice (Mental Health) MSc/PGDip</li>
  <li>Advanced Clinical Practice (Nursing) MSc/PGDip</li>
  <li>Clinical Health Psychology and Wellbeing MSc</li>
  <li>Healthcare Science (Audiology) BSc</li>
  <li>Social Work BA</li>
</ul>
"""


def test_mdx_table_keeps_ambiguous_postgraduate_scores_unset():
    flat, by_level = _parse_column_keyed_english_table(MDX_ENGLISH_TABLE)

    assert flat == {}
    assert by_level["undergraduate"]["ielts_overall"] == 6.0
    assert "postgraduate" not in by_level


def test_mdx_exception_profiles_override_named_courses_only():
    profiles = _parse_program_keyed_english_tables(MDX_ENGLISH_TABLE)

    assert _select_central_english_program(
        profiles,
        "Social Work BA Honours",
        "https://www.mdx.ac.uk/courses/undergraduate/social-work-ba-honours/",
    ) == {"ielts_overall": 7.0}
    assert _select_central_english_program(
        profiles,
        "Advanced Clinical Practice MSc",
        "https://www.mdx.ac.uk/courses/postgraduate/advanced-clinical-practice-msc/",
    ) == {}
    assert _select_central_english_program(
        profiles,
        "Social Work MSc",
        "https://www.mdx.ac.uk/courses/postgraduate/social-work-msc/",
    ) == {}
    assert _select_central_english_program(
        profiles,
        "Healthcare Science (Audiology) BSc Honours",
        "https://www.mdx.ac.uk/courses/undergraduate/healthcare-science-audiology-bsc-honours/",
    ) == {"ielts_overall": 7.0}
    assert _select_central_english_program(
        profiles,
        "International Business BA Honours",
        "https://www.mdx.ac.uk/courses/undergraduate/international-business-ba-honours/",
    ) == {}


@pytest.mark.asyncio
async def test_mdx_prefetch_clears_unsafe_generic_flat_score(monkeypatch):
    async def fake_fetch_html(_url):
        return MDX_ENGLISH_TABLE

    monkeypatch.setattr(central_pages, "fetch_html", fake_fetch_html)
    result = await central_pages.prefetch_central_pages(
        {
            "uniPages": {
                "entryPage": "https://www.mdx.ac.uk/study/english-language-requirements"
            }
        }
    )

    assert result["english"] == {}
    assert result["english_by_level"] == {
        "undergraduate": {"ielts_overall": 6.0}
    }
    assert len(result["english_by_program"]) == 5


@pytest.mark.asyncio
async def test_mdx_all_ambiguous_columns_clear_only_the_routed_flat_slot(
    monkeypatch,
):
    html = """
    <table>
      <tr>
        <th>Qualification</th>
        <th>Business postgraduate programmes</th>
        <th>Other postgraduate programmes</th>
      </tr>
      <tr>
        <td>IELTS</td><td>6.0</td><td>6.5</td>
      </tr>
    </table>
    """

    async def fake_fetch_html(_url):
        return html

    async def fake_parse(_html, _url):
        return {
            "ielts_overall": 6.0,
            "ielts_listening": 5.5,
            "ielts_reading": 6.0,
            "ielts_writing": 6.0,
            "ielts_speaking": 5.5,
            "pte_overall": 59.0,
        }

    monkeypatch.setattr(central_pages, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(
        central_pages, "_parse_english_page_html_async", fake_parse
    )
    result = await central_pages.prefetch_central_pages(
        {"uniPages": {"entryPage": "https://example.edu/english"}}
    )

    assert result["english"] == {"pte_overall": 59.0}
    assert result.get("english_by_level", {}) == {}


def test_exception_list_without_same_document_anchor_is_not_authoritative():
    unbound = MDX_ENGLISH_TABLE.replace(
        '<a href="#mdx-exceptional-courses">Exceptions</a>',
        "Exceptions",
    )
    assert _parse_program_keyed_english_tables(unbound) == []


def test_exception_link_immediately_preceding_table_binds_named_section():
    table_start = MDX_ENGLISH_TABLE.index("<table>")
    table_end = MDX_ENGLISH_TABLE.index("</table>") + len("</table>")
    table = MDX_ENGLISH_TABLE[table_start:table_end].replace(
        '<a href="#mdx-exceptional-courses">Exceptions</a>',
        "Exceptions",
    )
    remainder = MDX_ENGLISH_TABLE[table_end:]
    html = (
        '<div class="general-text">'
        '<p><a href="#mdx-exceptional-courses">View exceptional courses</a></p>'
        f"{table}</div>{remainder}"
    )

    profiles = _parse_program_keyed_english_tables(html)
    assert _select_central_english_program(
        profiles, "Social Work BA Honours"
    ) == {"ielts_overall": 7.0}


def test_mdx_shared_recipe_applies_to_production_id_not_stub_identity():
    config = get_config_for_host(
        hostname="www.mdx.ac.uk",
        name="Middlesex University",
        scrape_url="https://www.mdx.ac.uk",
        university_id=83,
        db_scrape_config={},
    )

    assert config.slug == "mdx"
    assert (
        config.extraction.english.central_page
        == "https://www.mdx.ac.uk/study/english-language-requirements"
    )