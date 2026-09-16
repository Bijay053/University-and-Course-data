import hashlib
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


def _generated_stub(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"# Generated-stub-sha256: {digest}\n{body}"


def test_generated_id_stub_cannot_shadow_matching_shared_recipe(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path)
    _write(
        tmp_path / "portable_11.yaml",
        _generated_stub("""# Hostname: portable.edu
# Auto-generated: 2026-09-03
# This stub was created automatically on the first scrape of this university.
discovery: {}
extraction:
  fees:
    default_currency: USD
"""),
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


def test_generated_id_stub_cannot_shadow_matching_numeric_recipe(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path)
    _write(
        tmp_path / "portable_11.yaml",
        _generated_stub("""# Hostname: portable.edu
# Auto-generated: 2026-09-05
# This stub was created automatically on the first scrape of this university.
discovery:
  allow_url_patterns: ["/wrong/"]
"""),
    )
    _write(
        tmp_path / "portable_2215.yaml",
        """hostname_guard: portable.edu
discovery:
  allow_url_patterns: ["/verified-course/"]
""",
    )

    config = _load(slug="portable", host="portable.edu", university_id=11)

    assert config.discovery.allow_url_patterns == ["/verified-course/"]


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


def test_runtime_generated_overlay_preserves_only_non_conflicting_settings(
    monkeypatch, tmp_path
) -> None:
    unis = tmp_path / "unis"
    runtime_unis = tmp_path / "runtime_unis"
    unis.mkdir()
    runtime_unis.mkdir()
    monkeypatch.setattr(loader, "_UNIS_DIR", unis)
    monkeypatch.setattr(loader, "_RUNTIME_UNIS_DIR", runtime_unis)
    _write(
        unis / "portable_11.yaml",
        """hostname_guard: portable.edu
discovery:
  bfs_page_budget: 9
extraction:
  fees:
    default_currency: CAD
""",
    )
    _write(
        runtime_unis / "portable_11.yaml",
        """discovery:
  bfs_page_budget: 3
  max_candidates: 77
extraction:
  fees:
    default_currency: USD
""",
    )

    config = _load(slug="portable", host="portable.edu", university_id=11)

    assert config.discovery.bfs_page_budget == 9
    assert config.discovery.max_candidates == 77
    assert config.extraction.fees.default_currency == "CAD"


def test_generated_stub_identity_fails_after_manual_edit(tmp_path) -> None:
    body = """# Hostname: portable.edu
# Auto-generated: 2026-09-16
# This stub was created automatically on the first scrape of this university.
discovery: {}
"""
    path = tmp_path / "portable_11.yaml"
    _write(path, _generated_stub(body))
    assert loader._is_generated_stub(path)

    path.write_text(
        path.read_text().replace(
            "discovery: {}", "discovery: {bfs_page_budget: 9}"
        )
    )
    assert not loader._is_generated_stub(path)


def test_untouched_legacy_generated_stub_identity_is_recognized(tmp_path) -> None:
    path = tmp_path / "portable_11.yaml"
    _write(
        path,
        """# Portable Test University
# Hostname: portable.edu
# Country: United States  |  Currency: USD
# Slug: portable
# Auto-generated: 2026-09-16
#
# This stub was created automatically on the first scrape of this university.
# Review and expand it to improve discovery and extraction quality.
# See scraper_config/defaults.yaml for all available options.

discovery: {}

extraction:
  fees:
    default_currency: USD
""",
    )

    assert loader._is_generated_stub(path)


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


def test_verified_yaml_paths_override_stale_admin_discovery_rules(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path)
    _write(
        tmp_path / "portable.yaml",
        """hostname_guard: portable.edu
locked_config_paths:
  - discovery.sitemap_url
  - discovery.allow_url_patterns
discovery:
  sitemap_url: https://portable.edu/study-sitemap.xml
  allow_url_patterns: ["/verified-course/"]
  bfs_page_budget: 2
""",
    )

    config = loader.load_uni_config(
        slug="portable",
        name="Portable Test University",
        scrape_url="https://portable.edu/",
        university_id=11,
        db_scrape_config={
            "admin_config": {
                "discovery": {
                    "sitemap_url": "https://portable.edu/wrong.xml",
                    "allow_url_patterns": ["/online/category/"],
                    "bfs_page_budget": 9,
                }
            }
        },
    )

    assert config.discovery.sitemap_url == "https://portable.edu/study-sitemap.xml"
    assert config.discovery.allow_url_patterns == ["/verified-course/"]
    assert config.discovery.bfs_page_budget == 9