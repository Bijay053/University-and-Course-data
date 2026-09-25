import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "capture_full_review_evidence.py"
spec = importlib.util.spec_from_file_location("capture_full_review_evidence", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(module)


def test_digest_is_stable_and_does_not_return_contents():
    first = module.digest_row({"description": "private text", "amount": 1})
    second = module.digest_row({"amount": 1, "description": "private text"})
    assert first == second
    assert "private" not in first


def test_baseline_allows_new_staged_rows_but_detects_changed_existing_rows():
    baseline = {"scraped_courses": {"rows": [{"id": 1, "sha256": "old"}]},
                "published_courses": {"rows": []}}
    current = {"scraped_courses": {"rows": [{"id": 1, "sha256": "new"}, {"id": 2, "sha256": "added"}]},
               "published_courses": {"rows": []}}
    result = module._compare(baseline, current)
    assert result["scraped_courses"]["changed_existing_ids"] == [1]
    assert result["scraped_courses"]["new_ids"] == [2]


@pytest.mark.parametrize("changed_field", [
    "fee_sha256",
    "english_requirement_sha256",
    "academic_requirement_sha256",
])
def test_baseline_detects_related_hash_changes_with_unchanged_row_hash(changed_field):
    baseline = {
        "scraped_courses": {
            "rows": [{"id": 1, "sha256": "same", "evidence_sha256": ["old"]}]
        },
        "published_courses": {
            "rows": [{
                "id": 2,
                "sha256": "same",
                "fee_sha256": ["fee"],
                "english_requirement_sha256": ["english"],
                "academic_requirement_sha256": ["academic"],
            }]
        },
    }
    published_row = {
        "id": 2,
        "sha256": "same",
        "fee_sha256": ["fee"],
        "english_requirement_sha256": ["english"],
        "academic_requirement_sha256": ["academic"],
    }
    published_row[changed_field] = ["changed"]
    current = {
        "scraped_courses": {
            "rows": [{"id": 1, "sha256": "same", "evidence_sha256": ["new"]}]
        },
        "published_courses": {"rows": [published_row]},
    }

    result = module._compare(baseline, current)

    assert result["scraped_courses"]["changed_existing_ids"] == [1]
    assert result["published_courses"]["changed_existing_ids"] == [2]
    assert result["scraped_courses"]["preserved_unchanged_ids"] == []
    assert result["published_courses"]["preserved_unchanged_ids"] == []


def test_safe_reason_omits_entire_secret_bearing_free_text():
    secret = "actual-secret-value"
    reason = module._safe_reason(f"request failed token={secret} while retrying")

    assert reason == "[redacted secret-bearing reason]"
    assert secret not in reason


def test_config_evidence_uses_strict_load_and_reports_effective_flags(monkeypatch, tmp_path):
    yaml_path = tmp_path / "example.yaml"
    yaml_path.write_text("locked_config_paths: []\n", encoding="utf-8")
    calls = {}
    data = {
        "discovery": {
            "official_catalogue_fallback": True,
            "searchstax": None,
        },
        "extraction": {
            "degree_level": {"irrelevant": True},
            "staging": {"skip_degree_qualifier_check": True},
            "fees": {},
        },
    }

    def get_config_for_host(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(model_dump=lambda **_kwargs: data)

    monkeypatch.setattr(module.config_loader, "get_config_for_host", get_config_for_host)
    monkeypatch.setattr(
        module.config_loader, "_select_uni_yaml", lambda **_kwargs: (yaml_path, False)
    )
    monkeypatch.setattr(module.config_loader, "_RUNTIME_UNIS_DIR", tmp_path)

    evidence = module._config_evidence({
        "id": 7,
        "name": "Example University",
        "scrape_url": "https://example.edu/courses",
        "scrape_config": {},
    })

    assert calls["strict"] is True
    assert calls["create_missing_stub"] is False
    assert evidence["extraction"]["degree_level_exception"] is True
    assert evidence["locked_recipe"]["searchstax_status"] == {
        "configured": False,
        "enabled": False,
        "disabled_by_official_catalogue_fallback": True,
    }