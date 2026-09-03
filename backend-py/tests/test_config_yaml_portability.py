from pathlib import Path

from app.services.scraper.config import loader


def _load(*, slug: str, host: str, university_id: int):
    return loader.load_uni_config(
        slug=slug,
        name="Portable Test University",
        scrape_url=f"https://{host}/",
        university_id=university_id,
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_generated_id_stub_cannot_shadow_matching_shared_recipe(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path)
    _write(
        tmp_path / "portable_11.yaml",
        """# Hostname: portable.edu
# Auto-generated: 2026-09-03
# This stub was created automatically on the first scrape of this university.
discovery: {}
extraction:
  fees:
    default_currency: USD
""",
    )
    _write(
        tmp_path / "portable.yaml",
        """hostname_guard: portable.edu
discovery:
  allow_url_patterns: ["/programme/"]
extraction:
  study_mode:
    suppress_nav_rule: true
""",
    )

    config = _load(slug="portable", host="portable.edu", university_id=11)

    assert config.discovery.allow_url_patterns == ["/programme/"]
    assert config.extraction.study_mode.suppress_nav_rule is True


def test_unique_hostname_recipe_loads_when_database_id_differs(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path)
    _write(
        tmp_path / "portable_2215.yaml",
        """# Hostname: portable.edu
discovery:
  allow_url_patterns: ["/course/"]
""",
    )

    config = _load(slug="portable", host="portable.edu", university_id=11)

    assert config.discovery.allow_url_patterns == ["/course/"]
    assert not (tmp_path / "portable_11.yaml").exists()


def test_hostname_recipe_matching_fails_closed_when_ambiguous(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path)
    for university_id, pattern in ((100, "/one/"), (200, "/two/")):
        _write(
            tmp_path / f"portable_{university_id}.yaml",
            f"""# Hostname: portable.edu
discovery:
  allow_url_patterns: ["{pattern}"]
""",
        )

    config = _load(slug="portable", host="portable.edu", university_id=11)

    assert config.discovery.allow_url_patterns == []
    assert (tmp_path / "portable_11.yaml").exists()