from app.services.scraper.extractors.flinders_html import (
    compact_course_html,
    is_flinders_host,
)


def test_flinders_host_detection():
    assert is_flinders_host("https://www.flinders.edu.au/study/courses/example")
    assert not is_flinders_host("https://example.edu.au/study/courses/example")


def test_compaction_keeps_metadata_hero_and_fast_facts():
    html = """
    <html><head><title>Bachelor of Testing</title>
      <meta name="description" content="Authoritative summary">
    </head><body>
      <nav>thousands of irrelevant links</nav>
      <div class="section"><div><h1>Bachelor of Testing</h1></div></div>
      <div class="courses-fast-facts-v2">
        <div>IELTS overall 6.0</div><div>Annual fee $39,000</div>
        <div>CRICOS 058295A</div>
      </div>
      <div class="related-courses">Wrong sibling fee $99,000</div>
      <footer>irrelevant chrome</footer>
    </body></html>
    """
    result = compact_course_html(html)
    assert "Bachelor of Testing" in result
    assert "Authoritative summary" in result
    assert "Annual fee $39,000" in result
    assert "058295A" in result
    assert "Wrong sibling fee" not in result
    assert "irrelevant chrome" not in result


def test_compaction_fails_open_when_expected_component_missing():
    html = "<html><body><h1>Unexpected template</h1></body></html>"
    assert compact_course_html(html) == html