"""
Tests for Ulster University year-dedup fixes.

Root cause: Ulster's sitemap contains BOTH /202627/ and /202728/ variants of
every course, with DIFFERENT numeric database IDs each year:
  /courses/202627/law-40288  (2026/27 intake, ID=40288)
  /courses/202728/law-41910  (2027/28 intake, ID=41910)

Two bugs in the year_dedup engine caused dedup to silently skip all Ulster
URLs and stage both variants as separate courses:

  Bug 1 — Regex gap: _YEAR_SEG_Y only matched 4-digit years (2026) or
           hyphenated pairs (2026-27).  Ulster's 6-digit compact format
           (202627) matched neither, so _url_year_y returned None → ALL
           Ulster URLs landed in the "no year" bucket and were kept verbatim.

  Bug 2 — ID mismatch: even with the year stripped, law-40288 ≠ law-41910,
           so the dedup key differed and both rows were kept.

Fixes:
  1. Add compact-year group to _YEAR_SEG_Y: 20\d{4} matches 202627 / 202728.
  2. Add year_dedup_strip_trailing_id (schema + YAML) — after year stripping,
     also strip trailing -\d{4,6} so both reduce to /courses-YYYY/law.

Also: PGCE and degree-apprenticeship courses are domestic-only in the UK and
are now blocked in discovery via block_url_patterns.
"""

import re
import sys
import os

import pytest
import yaml

ULSTER_YAML = os.path.join(
    os.path.dirname(__file__),
    "..", "scraper_config", "unis", "ulster_2176.yaml",
)

# ── Replicate the fixed regex exactly as it appears in orchestrator.py ──────

YEAR_SEG_FIXED = re.compile(
    r"[/_\-](20\d{2}-\d{2})(?=[/_\?\#]|$)"   # YYYY-YY pair first
    r"|[/_\-](20\d{4})(?=[/_\?\#]|$)"          # YYYYNN compact second (202627)
    r"|[/_\-](20\d{2})(?=[/_\?\#]|$)"          # bare YYYY third
)


def _url_year(u: str):
    """Return the start-year integer from a URL, or None if no year found."""
    m = YEAR_SEG_FIXED.search(u)
    if m:
        raw = m.group(1) or m.group(2) or m.group(3) or ""
        return int(raw[:4])
    return None


def _strip_year(u: str, strip_trailing_id: bool = False) -> str:
    """Strip the year segment (and optionally the trailing numeric ID)."""
    stripped = YEAR_SEG_FIXED.sub("-YYYY", u)
    if strip_trailing_id:
        stripped = re.sub(r"-\d{4,6}(?=[/?#]|$)", "", stripped)
    return stripped


# ── YAML loading helper ──────────────────────────────────────────────────────

def _load_ulster_yaml():
    with open(ULSTER_YAML) as f:
        return yaml.safe_load(f)


# ── Bug 1 tests: 6-digit compact year recognition ────────────────────────────

class TestCompactYearRecognition:
    """_YEAR_SEG_Y must extract a year from Ulster's 6-digit compact format."""

    @pytest.mark.parametrize("url,expected_year", [
        ("https://www.ulster.ac.uk/courses/202627/law-40288", 2026),
        ("https://www.ulster.ac.uk/courses/202728/law-41910", 2027),
        ("https://www.ulster.ac.uk/courses/202627/real-estate-40277", 2026),
        ("https://www.ulster.ac.uk/courses/202728/pgce-primary-45545", 2027),
        ("https://www.ulster.ac.uk/courses/202829/medicine-50000", 2028),
    ])
    def test_compact_year_extracted(self, url, expected_year):
        assert _url_year(url) == expected_year, (
            f"Expected year {expected_year} from {url}, got {_url_year(url)}"
        )

    def test_compact_year_was_not_matched_by_old_regex(self):
        OLD_YEAR_SEG = re.compile(
            r"[/_\-](20\d{2}-\d{2})(?=[/_\?\#]|$)"
            r"|[/_\-](20\d{2})(?=[/_\?\#]|$)"
        )
        url = "https://www.ulster.ac.uk/courses/202627/law-40288"
        m = OLD_YEAR_SEG.search(url)
        if m:
            raw = m.group(1) or m.group(2) or ""
        else:
            raw = ""
        assert raw == "", (
            "Old regex should NOT match 202627 (that was the bug); "
            f"got raw={raw!r}"
        )

    def test_hyphenated_pair_still_works(self):
        url = "https://www.bcu.ac.uk/courses/marketing-msc-2026-27"
        assert _url_year(url) == 2026

    def test_bare_year_still_works(self):
        url = "https://www.example.ac.uk/programmes/mba-2027/"
        assert _url_year(url) == 2027

    def test_no_year_returns_none(self):
        url = "https://www.ulster.ac.uk/courses/law/overview"
        assert _url_year(url) is None

    def test_compact_year_ordering_202728_newer_than_202627(self):
        assert _url_year("https://www.ulster.ac.uk/courses/202728/law-41910") > \
               _url_year("https://www.ulster.ac.uk/courses/202627/law-40288")


# ── Bug 2 tests: trailing ID stripping ───────────────────────────────────────

class TestTrailingIdStripping:
    """year_dedup_strip_trailing_id must collapse /courses-YYYY/law-40288 and
    /courses-YYYY/law-41910 to the same /courses-YYYY/law key."""

    @pytest.mark.parametrize("url_a,url_b", [
        (
            "https://www.ulster.ac.uk/courses/202627/law-40288",
            "https://www.ulster.ac.uk/courses/202728/law-41910",
        ),
        (
            "https://www.ulster.ac.uk/courses/202627/real-estate-40277",
            "https://www.ulster.ac.uk/courses/202728/real-estate-45100",
        ),
        (
            "https://www.ulster.ac.uk/courses/202627/computing-systems-39327",
            "https://www.ulster.ac.uk/courses/202728/computing-systems-45999",
        ),
    ])
    def test_same_dedup_key_with_id_stripping(self, url_a, url_b):
        key_a = _strip_year(url_a, strip_trailing_id=True)
        key_b = _strip_year(url_b, strip_trailing_id=True)
        assert key_a == key_b, (
            f"Expected same dedup key:\n  {url_a} → {key_a}\n  {url_b} → {key_b}"
        )

    def test_different_dedup_key_without_id_stripping(self):
        url_a = "https://www.ulster.ac.uk/courses/202627/law-40288"
        url_b = "https://www.ulster.ac.uk/courses/202728/law-41910"
        key_a = _strip_year(url_a, strip_trailing_id=False)
        key_b = _strip_year(url_b, strip_trailing_id=False)
        assert key_a != key_b, (
            "Without ID stripping the keys should differ (this was the bug)"
        )

    def test_short_numeric_tokens_not_stripped(self):
        url = "https://www.ulster.ac.uk/courses/202728/level-3-diploma-99"
        stripped = _strip_year(url, strip_trailing_id=True)
        assert "level-3" in stripped, (
            "Short tokens like 'level-3' (1 digit) should NOT be stripped"
        )

    def test_id_at_end_of_url_stripped(self):
        url = "https://www.ulster.ac.uk/courses/202728/medicine-41999"
        key = _strip_year(url, strip_trailing_id=True)
        assert not key.endswith("-41999"), f"Trailing 5-digit ID should be stripped; got {key}"
        assert key.endswith("medicine"), f"Expected key to end with 'medicine'; got {key}"

    def test_id_before_query_string_stripped(self):
        url = "https://www.ulster.ac.uk/courses/202728/law-41910?year=2027"
        key = _strip_year(url, strip_trailing_id=True)
        assert "-41910" not in key, f"ID before '?' should be stripped; got {key}"


# ── End-to-end dedup simulation ───────────────────────────────────────────────

class TestEndToEndDedup:
    """Simulate the orchestrator's keep_latest dedup with both fixes applied."""

    def _dedup_keep_latest(self, links: list[dict], strip_id: bool = True) -> list[dict]:
        from collections import defaultdict
        groups = defaultdict(list)
        no_year = []
        for lk in links:
            url = lk.get("url", "")
            yr = _url_year(url)
            if yr is None:
                no_year.append(lk)
            else:
                groups[_strip_year(url, strip_id)].append((yr, lk))
        kept = list(no_year)
        for versions in groups.values():
            winner = sorted(versions, key=lambda x: x[0], reverse=True)[0][1]
            kept.append(winner)
        return kept

    def test_law_deduped_to_one_prefers_202728(self):
        links = [
            {"url": "https://www.ulster.ac.uk/courses/202627/law-40288", "name": "Law 202627"},
            {"url": "https://www.ulster.ac.uk/courses/202728/law-41910", "name": "Law 202728"},
        ]
        result = self._dedup_keep_latest(links)
        assert len(result) == 1
        assert "202728" in result[0]["url"]

    def test_multiple_courses_all_deduped(self):
        links = [
            {"url": "https://www.ulster.ac.uk/courses/202627/law-40288"},
            {"url": "https://www.ulster.ac.uk/courses/202728/law-41910"},
            {"url": "https://www.ulster.ac.uk/courses/202627/real-estate-40277"},
            {"url": "https://www.ulster.ac.uk/courses/202728/real-estate-45100"},
            {"url": "https://www.ulster.ac.uk/courses/202627/medicine-39000"},
            {"url": "https://www.ulster.ac.uk/courses/202728/medicine-46000"},
        ]
        result = self._dedup_keep_latest(links)
        assert len(result) == 3, f"Expected 3 unique courses, got {len(result)}"
        for r in result:
            assert "202728" in r["url"], f"Expected 202728 variant to win: {r['url']}"

    def test_single_year_course_always_kept(self):
        links = [
            {"url": "https://www.ulster.ac.uk/courses/202628/rare-course-99999"},
        ]
        result = self._dedup_keep_latest(links)
        assert len(result) == 1

    def test_no_year_url_always_kept(self):
        links = [
            {"url": "https://www.ulster.ac.uk/courses/overview"},
            {"url": "https://www.ulster.ac.uk/courses/202728/law-41910"},
        ]
        result = self._dedup_keep_latest(links)
        assert len(result) == 2

    def test_without_id_stripping_gives_duplicates(self):
        links = [
            {"url": "https://www.ulster.ac.uk/courses/202627/law-40288"},
            {"url": "https://www.ulster.ac.uk/courses/202728/law-41910"},
        ]
        result = self._dedup_keep_latest(links, strip_id=False)
        assert len(result) == 2, (
            "Without ID stripping both year-variants should survive (the bug)"
        )


# ── YAML config tests ─────────────────────────────────────────────────────────

class TestUlsterYamlConfig:
    """Verify ulster_2176.yaml has the correct dedup and block settings."""

    def test_year_dedup_mode_keep_latest(self):
        cfg = _load_ulster_yaml()
        assert cfg["discovery"]["year_dedup_mode"] == "keep_latest"

    def test_year_dedup_strip_trailing_id_enabled(self):
        cfg = _load_ulster_yaml()
        assert cfg["discovery"]["year_dedup_strip_trailing_id"] is True

    def test_pgce_blocked_in_discovery(self):
        cfg = _load_ulster_yaml()
        patterns = cfg["discovery"].get("block_url_patterns", [])
        assert any("pgce" in p for p in patterns), (
            "PGCE (UK domestic teacher-training) must be blocked in discovery"
        )

    def test_degree_apprenticeship_blocked_in_discovery(self):
        cfg = _load_ulster_yaml()
        patterns = cfg["discovery"].get("block_url_patterns", [])
        assert any("degree-apprenticeship" in p for p in patterns), (
            "Degree Apprenticeship (employer-funded, domestic-only) must be blocked"
        )

    def test_pgce_pattern_matches_ulster_pgce_urls(self):
        cfg = _load_ulster_yaml()
        patterns = cfg["discovery"].get("block_url_patterns", [])
        pgce_url = "https://www.ulster.ac.uk/courses/202728/pgce-primary-education-45545"
        matched = any(re.search(p, pgce_url) for p in patterns)
        assert matched, f"PGCE URL not blocked by patterns {patterns}"

    def test_degree_apprenticeship_pattern_matches_ulster_urls(self):
        cfg = _load_ulster_yaml()
        patterns = cfg["discovery"].get("block_url_patterns", [])
        da_url = "https://www.ulster.ac.uk/courses/202627/computing-systems-degree-apprenticeship-39327"
        matched = any(re.search(p, da_url) for p in patterns)
        assert matched, f"Degree Apprenticeship URL not blocked by patterns {patterns}"

    def test_non_blocked_course_not_matched(self):
        cfg = _load_ulster_yaml()
        patterns = cfg["discovery"].get("block_url_patterns", [])
        normal_url = "https://www.ulster.ac.uk/courses/202728/law-41910"
        matched = any(re.search(p, normal_url) for p in patterns)
        assert not matched, f"Normal Law URL should NOT be blocked by patterns {patterns}"

    def test_schema_field_accepted_by_pydantic(self):
        from app.services.scraper.config.schema import DiscoveryConfig
        cfg = DiscoveryConfig(
            year_dedup_mode="keep_latest",
            year_dedup_strip_trailing_id=True,
        )
        assert cfg.year_dedup_strip_trailing_id is True

    def test_schema_field_default_is_false(self):
        from app.services.scraper.config.schema import DiscoveryConfig
        cfg = DiscoveryConfig()
        assert cfg.year_dedup_strip_trailing_id is False
