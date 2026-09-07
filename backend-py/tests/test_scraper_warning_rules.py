from app.services.scraper.warning_rules import should_emit_category_pages_warning


def test_cqu_degree_qualifier_override_suppresses_category_page_warning() -> None:
    assert not should_emit_category_pages_warning(
        category_count=14,
        total_count=14,
        skip_degree_qualifier_check=True,
    )


def test_small_unoverridden_category_only_result_still_warns() -> None:
    assert should_emit_category_pages_warning(
        category_count=14,
        total_count=14,
        skip_degree_qualifier_check=False,
    )


def test_large_catalogue_does_not_get_category_only_warning() -> None:
    assert not should_emit_category_pages_warning(
        category_count=182,
        total_count=182,
        skip_degree_qualifier_check=False,
    )