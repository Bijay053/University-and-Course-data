#!/usr/bin/env python3
"""Multi-university regression sweep — compare two baseline snapshots.

Compares the output of ``capture_baseline.py`` before and after a code
change to detect unexpected regressions across all universities.

WORKFLOW
--------
1. Capture "before" baseline (current prod code, no changes):
       cd backend-py
       PYTHONPATH=. python scripts/capture_baseline.py --out-dir baselines/before/

2. Apply code change (e.g. Phase 3 gating PR).

3. Re-run scrapes for all affected unis on prod (or run a dry-run via
   ``capture_baseline.py --dry-run`` to confirm which unis have fresh jobs).

4. Capture "after" baseline:
       PYTHONPATH=. python scripts/capture_baseline.py --out-dir baselines/after/

5. Run this sweep:
       PYTHONPATH=. python scripts/regression_sweep.py \\
           --before baselines/before/ \\
           --after  baselines/after/  \\
           --expected-slugs acap        # slugs explicitly migrated in this PR

   Exit code 0 → no unexpected diffs.
   Exit code 1 → unexpected diffs found — block the merge.

SELF-TEST (determinism check)
------------------------------
Run the sweep comparing a snapshot directory against itself.  Must produce
zero diffs.  If it doesn't, the sweep has a bug; fix it before using it to
validate Phase 3.

       PYTHONPATH=. python scripts/regression_sweep.py \\
           --before baselines/before/ \\
           --after  baselines/before/ \\
           --self-test

FIELDS COMPARED
---------------
Per course (keyed by course name + URL, falling back to DB id):
  name, level, duration, location, intakes, study_mode,
  fee_international, fee_term, currency, ielts, pte, toefl,
  extraction_method (per-field provenance dict)

A change in extraction_method without a change in the extracted value is
flagged as a WARNING, not an ERROR, because method regressions are lower
severity but still worth reviewing.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Field groups ──────────────────────────────────────────────────────────────

_VALUE_FIELDS: tuple[str, ...] = (
    "name",
    "level",
    "duration",
    "location",
    "intakes",
    "study_mode",
    "fee_international",
    "fee_term",
    "currency",
    "ielts",
    "pte",
    "toefl",
)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FieldDiff:
    field: str
    before: Any
    after: Any
    is_method_only: bool = False  # True when only extraction_method changed


@dataclass
class CourseDiff:
    key: str                    # course identity key used for matching
    kind: str                   # "changed" | "added" | "removed"
    field_diffs: list[FieldDiff] = field(default_factory=list)


@dataclass
class UniDiff:
    slug: str
    uni_id: int
    before_count: int
    after_count: int
    course_diffs: list[CourseDiff] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.course_diffs) == 0

    @property
    def count_changed(self) -> int:
        return sum(1 for d in self.course_diffs if d.kind == "changed")

    @property
    def count_added(self) -> int:
        return sum(1 for d in self.course_diffs if d.kind == "added")

    @property
    def count_removed(self) -> int:
        return sum(1 for d in self.course_diffs if d.kind == "removed")


# ── Snapshot loading ──────────────────────────────────────────────────────────

def _load_snapshots(directory: Path) -> dict[str, dict]:
    """Load all baseline JSON files from a directory.

    Returns a dict keyed by ``"{slug}_{uni_id}"`` (matches the filename
    convention from capture_baseline.py).
    """
    snapshots: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  WARN: could not load {path.name}: {exc}", file=sys.stderr)
            continue
        slug = data.get("slug", "unknown")
        uni_id = data.get("university_id", 0)
        key = f"{slug}_{uni_id}"
        if key in snapshots:
            # Prefer the most-recent file when there are multiple snapshots
            # for the same uni in the same directory (take last alphabetically,
            # which equals latest timestamp given the YYYYMMDD_HHMMSS prefix).
            pass
        snapshots[key] = data
    return snapshots


def _course_key(course: dict) -> str:
    """Derive a stable identity key for course matching.

    Prefer name for readability; fall back to DB id so courses without
    names are still matched correctly.
    """
    name = (course.get("name") or "").strip()
    db_id = course.get("id", "")
    return name if name else str(db_id)


# ── Per-course diff ───────────────────────────────────────────────────────────

def _diff_course(before: dict, after: dict) -> list[FieldDiff]:
    diffs: list[FieldDiff] = []

    for f in _VALUE_FIELDS:
        bv = before.get(f)
        av = after.get(f)
        if bv != av:
            diffs.append(FieldDiff(field=f, before=bv, after=av))

    # Check extraction_method changes (method-only regression)
    bm = before.get("extraction_method") or {}
    am = after.get("extraction_method") or {}
    method_fields_changed = {
        k for k in set(bm) | set(am)
        if bm.get(k) != am.get(k)
    }
    # Only report method changes where the VALUE field itself did NOT change
    # (pure method regression — same number, different path).
    value_changed_fields = {d.field for d in diffs}
    pure_method_changes = method_fields_changed - value_changed_fields
    for mf in sorted(pure_method_changes):
        diffs.append(FieldDiff(
            field=f"extraction_method.{mf}",
            before=bm.get(mf),
            after=am.get(mf),
            is_method_only=True,
        ))

    return diffs


# ── Per-uni diff ──────────────────────────────────────────────────────────────

def _diff_uni(slug: str, uni_id: int, before_snap: dict, after_snap: dict) -> UniDiff:
    before_courses: list[dict] = before_snap.get("courses") or []
    after_courses: list[dict] = after_snap.get("courses") or []

    before_by_key = {_course_key(c): c for c in before_courses}
    after_by_key  = {_course_key(c): c for c in after_courses}

    course_diffs: list[CourseDiff] = []

    # Removed courses
    for key in sorted(set(before_by_key) - set(after_by_key)):
        course_diffs.append(CourseDiff(key=key, kind="removed"))

    # Added courses
    for key in sorted(set(after_by_key) - set(before_by_key)):
        course_diffs.append(CourseDiff(key=key, kind="added"))

    # Changed courses (present in both)
    for key in sorted(set(before_by_key) & set(after_by_key)):
        field_diffs = _diff_course(before_by_key[key], after_by_key[key])
        if field_diffs:
            course_diffs.append(CourseDiff(key=key, kind="changed", field_diffs=field_diffs))

    return UniDiff(
        slug=slug,
        uni_id=uni_id,
        before_count=len(before_courses),
        after_count=len(after_courses),
        course_diffs=course_diffs,
    )


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_uni_diff(uni_diff: UniDiff, expected: bool) -> None:
    tag = "[EXPECTED]" if expected else "[UNEXPECTED]"
    status = "CLEAN" if uni_diff.is_clean else "DIRTY"
    print(
        f"\n  {tag} {uni_diff.slug} (uni_id={uni_diff.uni_id})  "
        f"before={uni_diff.before_count}  after={uni_diff.after_count}  "
        f"→ {status}"
    )
    if uni_diff.is_clean:
        return

    print(
        f"    added={uni_diff.count_added}  "
        f"removed={uni_diff.count_removed}  "
        f"changed={uni_diff.count_changed}"
    )

    for cd in uni_diff.course_diffs[:10]:   # cap output to first 10 per uni
        print(f"    [{cd.kind.upper()}] {cd.key!r}")
        for fd in cd.field_diffs[:5]:       # cap to first 5 fields
            prefix = "    (method-only)" if fd.is_method_only else "   "
            print(f"      {prefix} {fd.field}: {fd.before!r} → {fd.after!r}")

    if len(uni_diff.course_diffs) > 10:
        print(f"    ... ({len(uni_diff.course_diffs) - 10} more courses omitted)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:  # returns exit code
    parser = argparse.ArgumentParser(
        description=(
            "Compare two baseline snapshot directories produced by capture_baseline.py. "
            "Exit 0 if clean, 1 if unexpected regressions found."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--before", required=True, type=Path,
                        help="Directory of baseline snapshots BEFORE the change.")
    parser.add_argument("--after", required=True, type=Path,
                        help="Directory of baseline snapshots AFTER the change.")
    parser.add_argument(
        "--expected-slugs", nargs="*", default=[],
        metavar="SLUG",
        help=(
            "Slugs of universities explicitly migrated in this PR — diffs for "
            "these unis are 'expected' and do not cause a non-zero exit code. "
            "All other unis with diffs are 'unexpected' and block the merge."
        ),
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help=(
            "Assert that the sweep produces zero diffs (use --before and --after "
            "pointing to the same directory to verify sweep determinism)."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print clean unis in addition to dirty ones.",
    )
    args = parser.parse_args()

    before_dir: Path = args.before
    after_dir: Path  = args.after
    expected_slugs: set[str] = set(args.expected_slugs or [])

    if not before_dir.is_dir():
        print(f"ERROR: --before directory not found: {before_dir}", file=sys.stderr)
        return 2
    if not after_dir.is_dir():
        print(f"ERROR: --after directory not found: {after_dir}", file=sys.stderr)
        return 2

    before_snaps = _load_snapshots(before_dir)
    after_snaps  = _load_snapshots(after_dir)

    all_keys = sorted(set(before_snaps) | set(after_snaps))
    if not all_keys:
        print("ERROR: No snapshot files found in either directory.", file=sys.stderr)
        return 2

    print(f"\nRegression sweep: {len(before_snaps)} before / {len(after_snaps)} after snapshots")
    print(f"Expected slugs:   {sorted(expected_slugs) or '(none)'}")
    print("-" * 72)

    unexpected_dirty: list[UniDiff] = []
    expected_dirty:   list[UniDiff] = []
    clean_unis:       list[UniDiff] = []
    missing_before:   list[str]     = []
    missing_after:    list[str]     = []

    for key in all_keys:
        parts = key.rsplit("_", 1)
        slug   = parts[0]
        uni_id = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

        if key not in before_snaps:
            missing_before.append(key)
            continue
        if key not in after_snaps:
            missing_after.append(key)
            continue

        uni_diff = _diff_uni(slug, uni_id, before_snaps[key], after_snaps[key])
        is_expected = slug in expected_slugs

        if uni_diff.is_clean:
            clean_unis.append(uni_diff)
            if args.verbose:
                print(f"  [CLEAN]    {slug} (uni_id={uni_id})")
        elif is_expected:
            expected_dirty.append(uni_diff)
            _print_uni_diff(uni_diff, expected=True)
        else:
            unexpected_dirty.append(uni_diff)
            _print_uni_diff(uni_diff, expected=False)

    # Report missing
    for key in missing_before:
        print(f"\n  [MISSING-BEFORE] {key} — snapshot exists only in 'after'; "
              "cannot compare (new uni added this PR?)")
    for key in missing_after:
        print(f"\n  [MISSING-AFTER]  {key} — snapshot exists only in 'before'; "
              "likely removed or renamed.")

    # Summary
    print("\n" + "=" * 72)
    print(f"SUMMARY")
    print(f"  Clean unis:            {len(clean_unis)}")
    print(f"  Expected diffs:        {len(expected_dirty)}  (acknowledged)")
    print(f"  Unexpected diffs:      {len(unexpected_dirty)}")
    print(f"  Missing before:        {len(missing_before)}")
    print(f"  Missing after:         {len(missing_after)}")

    if args.self_test:
        if unexpected_dirty or expected_dirty or missing_before or missing_after:
            print("\nSELF-TEST FAILED: sweep is non-deterministic (diffs found against itself)")
            print("Fix the sweep before using it to validate a code change.")
            return 1
        print("\nSELF-TEST PASSED: zero diffs against itself — sweep is deterministic")
        return 0

    if unexpected_dirty:
        print(
            f"\nFAIL — {len(unexpected_dirty)} unexpected regression(s). "
            "Do not merge until all unexpected diffs are acknowledged."
        )
        return 1

    if expected_dirty:
        print(
            f"\nPASS (with acknowledged diffs) — {len(expected_dirty)} expected "
            f"diff(s) for {[d.slug for d in expected_dirty]}. "
            "Reviewer sign-off required before merge."
        )
    else:
        print("\nPASS — no unexpected regressions.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
