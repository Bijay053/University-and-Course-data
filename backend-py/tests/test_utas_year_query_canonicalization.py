from pathlib import Path

import yaml

from app.services.scraper.url_identity import (
    strip_and_deduplicate_course_query_parameters,
    strip_course_url_query_parameters,
)


UTAS_S4E = (
    "https://www.utas.edu.au/courses/sci-eng/courses/"
    "s4e-bachelor-of-science-with-honours"
)


def test_utas_stale_year_variant_rewrites_to_live_canonical_url() -> None:
    assert strip_course_url_query_parameters(
        f"{UTAS_S4E}?year=2025",
        ["year"],
    ) == UTAS_S4E


def test_query_cleanup_preserves_unconfigured_parameters() -> None:
    assert strip_course_url_query_parameters(
        f"{UTAS_S4E}?campaign=research&year=2026",
        ["year"],
    ) == f"{UTAS_S4E}?campaign=research"


def test_query_cleanup_matches_parameter_names_case_insensitively() -> None:
    assert strip_course_url_query_parameters(
        f"{UTAS_S4E}?Year=2025",
        ["year"],
    ) == UTAS_S4E


def test_utas_config_strips_year_query_before_extraction() -> None:
    config_path = Path(__file__).parents[1] / "scraper_config" / "unis" / "utas.yaml"
    config = yaml.safe_load(config_path.read_text())
    assert config["discovery"]["strip_query_parameters"] == ["year"]


def test_utas_year_variants_collapse_to_one_canonical_fetch() -> None:
    links = [
        {"url": f"{UTAS_S4E}?year=2025", "name": "S4E 2025"},
        {"url": f"{UTAS_S4E}?year=2026", "name": "S4E 2026"},
        {"url": UTAS_S4E, "name": "S4E canonical"},
    ]

    rewritten, rewrite_count, duplicate_count = (
        strip_and_deduplicate_course_query_parameters(links, ["year"])
    )

    assert rewritten == [{"url": UTAS_S4E, "name": "S4E 2025"}]
    assert rewrite_count == 2
    assert duplicate_count == 2