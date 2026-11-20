"""Production repair must recognize the same scoped programme as extraction."""
import pytest
from app.services.scraper.ai_repair_live import inspect_page

BASE = "https://www2.acadiau.ca/academics/graduate/"


def page(name, overview, widget=""):
    return (f'<body><header><h1>Acadia University - {name}</h1>'
            '<nav>Master of Science in Biology</nav></header>'
            f'<main id="main"><div class="overview"><p>{overview}</p></div>'
            f'<div id="requirements-fees">{widget}</div></main></body>')


@pytest.mark.parametrize("slug,name,text", [
    ("applied-geomatics", "Applied Geomatics",
     "The Master of Science in Applied Geomatics is a two-year program focused on "
     "collaborative research, coursework, and data interpretation."),
    ("biology", "Biology",
     "In Acadia's Master of Science in Biology you will enhance your expertise in "
     "modern research methods in biology and deepen your knowledge in your chosen area of study."),
    ("applied-kinesiology", "Applied Kinesiology",
     "You can choose to study in one of the three program streams featured within "
     "the Master of Applied Kinesiology degree:"),
])
def test_live_programme_overview_is_positive_owned_evidence(slug, name, text):
    result = inspect_page(BASE + slug + ".html", page(name, text))
    assert result["classification"] == "course", result["reason"]
    assert result["title"] == name
    assert result["programme_identity_evidence"] == text


@pytest.mark.parametrize("text", [
    "Applicants must hold a Master of Science in Biology and complete two years of research.",
    "Admission to the Master of Science in Biology requires coursework.",
    "The Master of Science in Chemistry is a two-year program.",
    "Biology is an exciting subject. Our staff also teach the Master of Science in Chemistry.",
    "Welcome to Biology.",
])
def test_admissions_or_other_awards_do_not_validate_programme(text):
    result = inspect_page(BASE + "biology.html", page("Biology", text))
    assert result["classification"] != "course"


def test_shared_all_programme_widget_is_not_positive_identity_or_field_evidence():
    widget = ("<p>The Master of Science in Biology is a two-year program.</p>"
              "<p>International tuition: CAD 99999</p><p>IELTS overall 9.0</p>")
    result = inspect_page(BASE + "biology.html", page("Biology", "Welcome to Biology.", widget))
    assert result["classification"] != "course"
    assert "99999" not in result["owned_html"]
    assert "9.0" not in result["owned_html"]


def test_scoping_does_not_erase_retained_transport_or_tuition_source():
    text = "The Master of Science in Biology is a two-year program."
    html = page("Biology", text, "<p>International tuition: CAD 99999</p>")
    result = inspect_page(BASE + "biology.html", html)
    assert result["classification"] == "course"
    assert result["html"] == html
    assert "99999" not in result["owned_html"]


@pytest.mark.parametrize("url", [
    "https://other.example/academics/graduate/biology.html",
    "https://www2.acadiau.ca/academics/graduate/current-students.html",
    "https://www2.acadiau.ca/admissions/biology.html",
])
def test_programme_adapter_cannot_validate_unrelated_or_support_routes(url):
    html = page("Biology", "The Master of Science in Biology is a two-year program.")
    result = inspect_page(url, html)
    assert "programme_identity_evidence" not in result
