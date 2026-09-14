from pathlib import Path

from app.services.scraper.orchestrator import _has_per_university_yaml


def test_cascade_recognizes_id_specific_yaml_recipe(tmp_path: Path) -> None:
    (tmp_path / "raffles-university_48.yaml").write_text("hostname_guard: example.edu\n")

    assert _has_per_university_yaml(tmp_path, "raffles-university") is True


def test_cascade_recognizes_shared_yaml_recipe(tmp_path: Path) -> None:
    (tmp_path / "raffles-university.yaml").write_text("hostname_guard: example.edu\n")

    assert _has_per_university_yaml(tmp_path, "raffles-university") is True


def test_cascade_does_not_match_another_slug(tmp_path: Path) -> None:
    (tmp_path / "raffles_48.yaml").write_text("hostname_guard: example.edu\n")

    assert _has_per_university_yaml(tmp_path, "raffles-university") is False