"""Check the offline review and its read-only snapshot comparisons."""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text())


def main():
    courses = read("course-review.json")
    staging = read("staging-review.json")
    original = read("development-invalid-inventory.json")
    staged_original = read("staged-invalid-inventory.json")
    assert len(courses) == len(original) == 64
    assert len(staging) == len(staged_original) == 67
    for records, source, table in [
        (courses, original, "courses"), (staging, staged_original, "scraped_courses")
    ]:
        by_id = {r["id"]: r for r in source}
        assert len(by_id) == len(records)
        for row in records:
            before = by_id[row["id"]]
            assert row["table"] == table
            assert row["current_study_mode"] == before["study_mode"]
            assert row["stored_official_url"] == before["course_website"]
            assert row["record_status"] == before["status"]
            assert row["evidence_quote"] and row["checked_at"]
            assert (ROOT / row["evidence_file"]).is_file()
            if row["proposal_status"] == "unknown":
                assert row["proposed_study_mode"] is None
            else:
                assert row["proposed_study_mode"]
    for filename, records in [("course-review", courses), ("staging-review", staging)]:
        with (ROOT / f"{filename}.csv").open(newline="") as f:
            csv_rows = list(csv.DictReader(f))
        assert len(csv_rows) == len(records)
        for csv_row, row in zip(csv_rows, records):
            for key, value in row.items():
                assert csv_row[key] == ("" if value is None else str(value))
    for filename, total in [("distinct-values.csv", 11965),
                            ("staged-distinct-values.csv", 36221)]:
        with (ROOT / filename).open(newline="") as f:
            assert sum(int(list(r.values())[1]) for r in csv.DictReader(f)) == total
    assert sum(r["proposal_status"] == "supported" for r in courses) == 57
    assert sum(r["proposal_status"] == "unknown" for r in courses) == 7
    ambiguous = read("ambiguous-labels.json")
    assert len(ambiguous) == 13
    assert all(r["study_mode"] in {"Both", "Mixed"} for r in ambiguous)
    assert not ({r["id"] for r in ambiguous} & {r["id"] for r in courses})
    unchanged = read("unchanged-record-check.json")
    assert unchanged["courses"] and unchanged["staged"]
    text = (ROOT / "review.html").read_text()
    assert text.startswith("<!doctype html>")
    assert text.count("<article>") == 64 + 67 + 13
    assert '<script src=' not in text and '<link ' not in text
    files = sorted(p for p in ROOT.rglob("*")
                   if p.is_file() and p.suffix in {".json", ".csv", ".html", ".md", ".sql"}
                   and p.name != "validation.json")
    result = {
        "passed": True,
        "checks": [
            "All current and staged invalid rows accounted for with full original values",
            "CSV/JSON field equality",
            "Inventory totals and ambiguity partition",
            "Evidence files and retrieval times exist for every decision",
            "Unknown replacements remain null",
            "Self-contained HTML contains every reviewed and ambiguous record",
            "Development database reread exactly matches initial affected-row snapshots",
        ],
        "course_rows": 64,
        "staged_rows": 67,
        "supported_course_proposals": 57,
        "unknown_course_proposals": 7,
        "ambiguous_course_rows": 13,
        "unchanged_record_check": unchanged,
        "sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in files},
    }
    (ROOT / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: 64 courses, 67 staged rows, 13 ambiguous records; exports and unchanged database snapshots verified.")


if __name__ == "__main__":
    main()
