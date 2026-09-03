from app.routers.universities import (
    _contains_encoded_html_entity,
    _decode_metadata_text,
    _metadata_title_segments,
)


def test_decodes_html_entities_in_metadata_text() -> None:
    assert _decode_metadata_text(
        "INTI International University &amp; Colleges"
    ) == "INTI International University & Colleges"


def test_decodes_numeric_dash_before_splitting_seo_title() -> None:
    assert _metadata_title_segments(
        "INTI International University &amp; Colleges &#8211; Your Future Built Today"
    ) == [
        "INTI International University & Colleges",
        "Your Future Built Today",
    ]


def test_normalizes_metadata_whitespace() -> None:
    assert _decode_metadata_text("Example&nbsp;&nbsp; University\n") == "Example University"


def test_detects_encoded_entities_in_existing_university_name() -> None:
    assert _contains_encoded_html_entity(
        "INTI International University &amp; Colleges &#8211; Your Future Built Today"
    )
    assert not _contains_encoded_html_entity(
        "INTI International University & Colleges"
    )