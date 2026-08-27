from app.services.scraper.html_compaction import (
    MIN_SOURCE_BYTES,
    compact_course_html,
)
from app.services.scraper.config.schema import ExtractionConfig
from app.services.html_compaction_counters import (
    get_html_compaction_stats,
    reset_html_compaction_stats,
)


def _chrome(label: str, count: int = 6_000) -> str:
    return "".join(
        f'<a class="menu-item-{i}" href="/section/{i}">{label}</a>'
        for i in range(count)
    )


def test_small_documents_are_not_reparsed_or_changed():
    html = "<html><body><h1>Small Course</h1></body></html>"
    assert compact_course_html(html) is html


def test_large_documents_are_disabled_by_default():
    html = (
        "<html><body><h1>Course</h1><nav>"
        + _chrome("navigation")
        + "</nav></body></html>"
    )
    assert compact_course_html(html) == html


def test_removes_semantic_chrome_but_keeps_course_data_and_json():
    html = (
        """<html><head><script type="application/ld+json">
        {"name":"Bachelor of Testing","courseMode":"full-time"}
        </script></head><body>
        <nav>""" + _chrome("navigation") + """</nav>
        <main><h1>Bachelor of Testing</h1>
        <p>Duration 3 years. IELTS 6.5. CRICOS 012345A.</p>
        <p>International tuition fee AUD 40,000.</p>
        <p>Entry requirements and March intake.</p></main>
        <footer>""" + _chrome("footer") + """</footer>
        </body></html>"""
    )
    result = compact_course_html(html, enabled=True)
    assert len(result) < len(html) * 0.5
    assert "<nav" not in result
    assert "<footer" not in result
    assert "navigation navigation" in result
    assert "footer footer" in result
    assert "Bachelor of Testing" in result
    assert "International tuition fee AUD 40,000" in result
    assert '"courseMode":"full-time"' in result


def test_compacted_chrome_preserves_critical_signal_text():
    html = (
        """<html><body><h1>Bachelor of Testing</h1>
        <main>Course description.</main>
        <footer>International tuition fee AUD 40,000. IELTS 6.5."""
        + _chrome("footer")
        + """</footer>
        </body></html>"""
    )
    result = compact_course_html(html, enabled=True)
    assert result != html
    assert "International tuition fee AUD 40,000. IELTS 6.5." in result


def test_compacted_chrome_preserves_duplicate_field_candidates():
    html = (
        """<html><body><h1>Bachelor of Testing</h1>
        <main>IELTS 6.5. March intake. Campus: City.</main>
        <footer>IELTS 7.0. September events. Campus life."""
        + _chrome("footer")
        + """</footer>
        </body></html>"""
    )
    result = compact_course_html(html, enabled=True)
    assert result != html
    assert "IELTS 6.5" in result
    assert "IELTS 7.0" in result
    assert result.index("IELTS 6.5") < result.index("IELTS 7.0")


def test_fails_open_when_custom_selector_would_remove_heading_structure():
    html = (
        """<html><body><main class="course-facts">
        <h1>Bachelor of Testing</h1><p>IELTS 6.5.</p>
        </main><footer>""" + _chrome("footer") + """</footer></body></html>"""
    )
    assert compact_course_html(
        html, enabled=True, extra_remove_selectors=[".course-facts"]
    ) == html


def test_custom_selector_and_disable_switch():
    html = (
        """<html><body><h1>Bachelor of Testing</h1>
        <main>Duration 3 years.</main>
        <section class="site-mega-menu">""" + _chrome("menu") + """</section>
        </body></html>"""
    )
    compacted = compact_course_html(
        html, enabled=True, extra_remove_selectors=[".site-mega-menu"]
    )
    assert "site-mega-menu" not in compacted
    assert compact_course_html(html, enabled=False) == html


def test_invalid_custom_selector_fails_open():
    html = (
        "<html><body><h1>Course</h1><nav>" + _chrome("menu") + "</nav></body></html>"
    )
    assert compact_course_html(
        html, enabled=True, extra_remove_selectors=["["]
    ) == html


def test_compaction_counts_accepted_and_fail_open_outcomes():
    reset_html_compaction_stats()
    accepted_html = (
        "<html><body><h1>Course</h1><nav>"
        + _chrome("menu")
        + "</nav><main>IELTS 6.5</main></body></html>"
    )
    failed_html = "<html><body><h1>Course</h1><nav>" + _chrome("menu") + "</nav></body></html>"

    compact_course_html(accepted_html, enabled=True)
    compact_course_html(failed_html, enabled=True, extra_remove_selectors=["["])

    stats = get_html_compaction_stats()
    assert stats["attempts"] == 2
    assert stats["accepted"] == 1
    assert stats["fail_open"] == 1
    assert stats["fail_open_reasons"] == {"invalid_selector": 1}
    assert stats["acceptance_rate"] == 0.5
    assert stats["reduction_rate"] > 0
    assert stats["elapsed_ms"] >= 0


def test_extraction_config_exposes_safe_overrides():
    defaults = ExtractionConfig()
    assert defaults.html_compaction_enabled is False
    assert defaults.html_compaction_remove_selectors == []

    overridden = ExtractionConfig(
        html_compaction_enabled=False,
        html_compaction_remove_selectors=[".site-mega-menu"],
    )
    assert overridden.html_compaction_enabled is False
    assert overridden.html_compaction_remove_selectors == [".site-mega-menu"]


def test_structured_descendants_are_never_flattened():
    html = (
        """<html><body><h1>Bachelor of Testing</h1>
        <main>IELTS 6.5.</main>
        <footer><script type="application/ld+json">
        {"name":"Bachelor of Testing","fee":40000}
        </script>"""
        + _chrome("footer")
        + """</footer><nav>"""
        + _chrome("navigation")
        + """</nav></body></html>"""
    )
    result = compact_course_html(html, enabled=True)
    assert '<script type="application/ld+json">' in result
    assert '{"name":"Bachelor of Testing","fee":40000}' in result
    assert "<footer>" in result
    assert "<nav>" not in result


def test_direct_structured_selectors_fail_open():
    html = (
        """<html><body><h1>Course</h1>
        <script type="application/ld+json">{"name":"Course"}</script>
        <table><tr><th>Fee</th><td>AUD 40,000</td></tr></table>
        <form><input name="audience" value="international">
        <select name="year"><option>2027</option></select></form>
        <nav>"""
        + _chrome("navigation")
        + """</nav></body></html>"""
    )
    for selector in ("script", "table", "form", "input", "select"):
        assert compact_course_html(
            html, enabled=True, extra_remove_selectors=[selector]
        ) == html