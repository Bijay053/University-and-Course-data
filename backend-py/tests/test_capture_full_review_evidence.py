import importlib.util
from pathlib import Path


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