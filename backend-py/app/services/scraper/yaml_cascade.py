"""Trial-and-error YAML cascade for new universities.

Problem
-------
We have ~43 hand-tuned per-uni YAMLs but need to onboard 1500+ unis. We
can't hand-write a YAML for each. Solution: when a new uni's scrape
underperforms (defaults.yaml + DB scrape_config produce <5 staged
courses OR avg completeness <50%), automatically scan the existing
YAMLs, rank them by structural similarity to the target uni
(CMS family > region > listing-URL shape), pick the top-ranked
candidate, and **clone it** to ``scraper_config/unis/<target_slug>.yaml``
with a clearly-marked auto-generated header.

What this module does
---------------------
- :func:`rank_candidate_yamls` — score every existing per-uni YAML
  against the target uni's fingerprint and return the ranked list.
- :func:`clone_yaml_for_target` — copy a winning YAML on disk, prepending
  a header comment that names the source and timestamp. **Refuses to
  overwrite an existing per-uni YAML** for the target slug.
- :func:`run_cascade_for_university` — async orchestration that fetches
  the target's listing page, fingerprints it, ranks candidates, clones
  the winner, and returns a structured CascadeResult.

What this module deliberately does NOT do (Phase 2)
---------------------------------------------------
- It does NOT execute trial scrapes against each candidate. Doing that
  cleanly requires transaction-isolated staging so trial rows never
  pollute ``scraped_courses``. The current implementation picks purely
  on signal-similarity heuristics, which is cheap, deterministic, and
  good enough for a starter YAML stub. Refining the cloned YAML is a
  follow-up task once the user runs a full scrape with it.

Safety invariants
-----------------
1. **Never overwrites an existing per-uni YAML.** If
   ``unis/<target_slug>.yaml`` exists, :func:`clone_yaml_for_target`
   returns a no-write result with reason ``"already_exists"``.
2. Listing-page fetch failures degrade gracefully: when fingerprinting
   fails, the cascade falls back to region+URL-shape ranking only.
3. Cloned YAMLs are tagged with a ``# AUTO-GENERATED`` header so the
   per-uni convention review can flag them for human refinement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.services.scraper.cms_fingerprint import (
    families_overlap,
    fingerprint,
    primary_family,
    region_for_hostname,
    shape_similarity,
    url_shape_signature,
)

log = logging.getLogger("uniportal.scraper.yaml_cascade")

# Filesystem layout — kept in sync with config/loader.py.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIGS_ROOT = _PROJECT_ROOT / "scraper_config"
_UNIS_DIR = _CONFIGS_ROOT / "unis"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateScore:
    """Per-candidate ranking detail returned by :func:`rank_candidate_yamls`."""
    slug: str
    yaml_path: str
    family_overlap: int
    same_region: bool
    shape_sim: float
    total_score: float
    candidate_url: str
    candidate_families: list[str] = field(default_factory=list)


@dataclass
class CascadeResult:
    """Structured outcome of :func:`run_cascade_for_university`."""
    success: bool
    reason: str
    target_slug: str
    target_url: str
    target_families: list[str]
    target_region: str
    candidates_scored: int
    winner: CandidateScore | None
    runners_up: list[CandidateScore]
    written_path: str | None
    skipped_existing: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["winner"] = asdict(self.winner) if self.winner else None
        d["runners_up"] = [asdict(r) for r in self.runners_up]
        return d


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_per_uni_yaml_files() -> list[Path]:
    if not _UNIS_DIR.exists():
        return []
    return sorted(p for p in _UNIS_DIR.glob("*.yaml") if p.is_file())


def _slug_from_yaml_path(path: Path) -> str:
    """``unis/acu.yaml`` → ``"acu"``; ``unis/torrens_5.yaml`` → ``"torrens"``."""
    stem = path.stem
    # Strip an ``_<digits>`` suffix used for university-id-specific overrides.
    if "_" in stem:
        head, _, tail = stem.rpartition("_")
        if tail.isdigit():
            return head
    return stem


def _safe_load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("yaml_cascade: failed to read %s: %s", path, exc)
        return {}


def _candidate_url_for_yaml(path: Path, yaml_data: dict[str, Any]) -> str:
    """Best-effort discovery URL for a candidate YAML.

    Prefers an explicit ``scrape_url`` field if the YAML defines one;
    otherwise constructs ``https://www.<slug>.edu.au/`` as a stable
    proxy used only for shape-fingerprinting (not network-fetched).
    """
    explicit = (yaml_data.get("scrape_url") or "").strip()
    if explicit:
        return explicit
    slug = _slug_from_yaml_path(path)
    return f"https://www.{slug}.edu.au/"


def _ensure_unis_dir() -> None:
    _UNIS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_candidate_yamls(
    *,
    target_url: str,
    target_html: str | None,
    exclude_slug: str | None = None,
) -> list[CandidateScore]:
    """Score every existing per-uni YAML against the target uni and return
    the ranked list (highest score first).

    Scoring formula::

        total = 5 * family_overlap            # CMS family is the strongest signal
              + 2 * (1 if same_region else 0)  # AU vs UK vs US locale
              + 3 * shape_similarity           # listing-URL structure (Jaccard)

    Family overlap is computed against the **target HTML's** fingerprint;
    candidate families are inferred from each candidate YAML's recorded
    ``scrape_url`` shape (we do NOT network-fetch every candidate page,
    that would balloon cost). When ``target_html`` is empty/None, the
    family component drops to 0 and ranking falls back to region+shape.

    Excludes the target slug itself (so the cascade doesn't recommend
    re-cloning the uni's own YAML if it already exists).
    """
    target_families_dict = fingerprint(target_html or "")
    target_families = list(target_families_dict.keys())
    target_host = (urlparse(target_url).hostname or "").lower() if target_url else ""
    target_region = region_for_hostname(target_host)
    target_shape = url_shape_signature(target_url or "")

    out: list[CandidateScore] = []
    for path in _list_per_uni_yaml_files():
        slug = _slug_from_yaml_path(path)
        if exclude_slug and slug == exclude_slug:
            continue
        yaml_data = _safe_load_yaml(path)
        cand_url = _candidate_url_for_yaml(path, yaml_data)
        cand_host = (urlparse(cand_url).hostname or "").lower()
        cand_region = region_for_hostname(cand_host)
        cand_shape = url_shape_signature(cand_url)

        # We don't have the candidate's HTML on hand — best we can do
        # for "candidate family" is record what FAMILY this YAML's slug
        # is conventionally for (e.g. federation → nextjs). For an MVP
        # we infer a single family per slug from a small lookup table
        # below; everything else falls into "generic" and only matches
        # the target via region + shape signals.
        cand_families = _CANDIDATE_FAMILY_HINTS.get(slug, [])

        family_score = families_overlap(target_families, cand_families)
        same_region = (target_region == cand_region) and (target_region != "global")
        shape_sim = shape_similarity(target_shape, cand_shape)

        total = (
            5 * family_score
            + 2 * (1 if same_region else 0)
            + 3 * shape_sim
        )

        out.append(CandidateScore(
            slug=slug,
            yaml_path=str(path),
            family_overlap=family_score,
            same_region=same_region,
            shape_sim=round(shape_sim, 3),
            total_score=round(total, 3),
            candidate_url=cand_url,
            candidate_families=list(cand_families),
        ))

    # Highest score first; stable secondary by slug for deterministic output.
    out.sort(key=lambda c: (-c.total_score, c.slug))
    return out


def clone_yaml_for_target(
    *,
    target_slug: str,
    source_yaml_path: str | Path,
    notes: str = "",
) -> tuple[bool, str, str | None]:
    """Copy ``source_yaml_path`` to ``unis/<target_slug>.yaml`` with a
    clearly-marked auto-generated header.

    Returns ``(success, reason, written_path)``.

    Refuses to overwrite — if the target file already exists, returns
    ``(False, "already_exists", existing_path)``. This is the single
    most important safety invariant in this module: we MUST NEVER blow
    away a hand-tuned per-uni YAML.

    The header comment names the source and a UTC timestamp so a human
    reviewer can later refine the file or revert it cleanly.
    """
    target_slug = (target_slug or "").strip().lower()
    if not target_slug:
        return False, "empty_target_slug", None
    src = Path(source_yaml_path)
    if not src.exists():
        return False, f"source_missing:{src}", None

    _ensure_unis_dir()
    dst = _UNIS_DIR / f"{target_slug}.yaml"
    if dst.exists():
        return False, "already_exists", str(dst)

    try:
        body = src.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return False, f"source_read_failed:{exc}", None

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header_lines = [
        f"# ─── AUTO-GENERATED YAML for {target_slug} ───────────────────",
        f"# Source:    {src.name}",
        f"# Generated: {ts}",
        f"# Generator: yaml_cascade.run_cascade_for_university()",
        "#",
        "# This file was cloned from the highest-scoring existing per-uni",
        "# YAML based on CMS-family + region + URL-shape signals. It is a",
        "# starting stub, NOT a finished tuning. After the next scrape:",
        "#   1. Compare staged completeness vs the source uni.",
        "#   2. Refine fee/intake/IELTS extractors per the per-uni-fix",
        "#      convention documented in replit.md.",
        "#   3. Delete this header once the file is hand-reviewed.",
    ]
    if notes:
        header_lines.append("#")
        for line in notes.splitlines():
            header_lines.append(f"# {line}")
    header_lines.append("# ─────────────────────────────────────────────────────────────")
    header = "\n".join(header_lines) + "\n\n"

    try:
        dst.write_text(header + body, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return False, f"write_failed:{exc}", None
    log.info("yaml_cascade: wrote %s (cloned from %s)", dst, src)
    return True, "ok", str(dst)


async def fetch_target_html(target_url: str, *, timeout_sec: float = 15.0) -> str | None:
    """Best-effort fetch of the target uni's listing page for fingerprinting.

    Uses :mod:`httpx` with a short timeout and a real-browser User-Agent.
    Returns ``None`` on any error — the caller falls back to region +
    URL-shape ranking when the page can't be fetched (e.g. Cloudflare).
    """
    if not target_url:
        return None
    try:
        import httpx
    except ImportError:
        log.warning("httpx not available for cascade fingerprint fetch")
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout_sec,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            resp = await client.get(target_url)
            resp.raise_for_status()
            return resp.text
    except Exception as exc:  # noqa: BLE001
        log.info("yaml_cascade: target fetch failed for %s: %s", target_url, exc)
        return None


async def run_cascade_for_university(
    *,
    university_id: int,
    university_name: str,
    target_url: str,
    target_slug: str,
    notes: str = "",
    skip_if_exists: bool = True,
) -> CascadeResult:
    """End-to-end cascade entry point.

    Steps
    -----
    1. If ``unis/<target_slug>.yaml`` already exists AND
       ``skip_if_exists=True`` → bail with ``skipped_existing=True``.
    2. Fetch the target listing page (best-effort).
    3. Fingerprint the page → CMS families.
    4. Score every existing per-uni YAML.
    5. Clone the top-scoring candidate to ``unis/<target_slug>.yaml``.

    Returns a :class:`CascadeResult` describing the outcome — JSON-safe
    via :meth:`CascadeResult.to_dict`. The function is idempotent: a
    second call sees the cloned YAML and returns ``skipped_existing``.
    """
    target_slug = (target_slug or "").strip().lower()
    target_url = (target_url or "").strip()

    if skip_if_exists and (_UNIS_DIR / f"{target_slug}.yaml").exists():
        log.info("yaml_cascade: %s.yaml already exists — skipping cascade", target_slug)
        return CascadeResult(
            success=False,
            reason="per_uni_yaml_already_exists",
            target_slug=target_slug,
            target_url=target_url,
            target_families=[],
            target_region=region_for_hostname(
                (urlparse(target_url).hostname or "").lower()
            ),
            candidates_scored=0,
            winner=None,
            runners_up=[],
            written_path=str(_UNIS_DIR / f"{target_slug}.yaml"),
            skipped_existing=True,
        )

    html = await fetch_target_html(target_url) if target_url else None
    families = list(fingerprint(html or "").keys())
    region = region_for_hostname((urlparse(target_url).hostname or "").lower())

    ranked = rank_candidate_yamls(
        target_url=target_url,
        target_html=html,
        exclude_slug=target_slug,
    )
    if not ranked:
        return CascadeResult(
            success=False,
            reason="no_candidate_yamls_found",
            target_slug=target_slug,
            target_url=target_url,
            target_families=families,
            target_region=region,
            candidates_scored=0,
            winner=None,
            runners_up=[],
            written_path=None,
            skipped_existing=False,
        )

    winner = ranked[0]
    # If even the top candidate scored zero (no family overlap, different
    # region, dissimilar shape), still clone — it's better than nothing
    # for an unknown new uni — but flag this in the reason for the UI.
    weak = winner.total_score <= 0.5
    notes_combined = (
        f"target_university_id={university_id}\n"
        f"target_university_name={university_name}\n"
        f"target_url={target_url}\n"
        f"target_families={families or ['none-detected']}\n"
        f"target_region={region}\n"
        f"chosen_candidate={winner.slug} (score={winner.total_score})\n"
    )
    if notes:
        notes_combined += notes + "\n"

    ok, reason, path = clone_yaml_for_target(
        target_slug=target_slug,
        source_yaml_path=winner.yaml_path,
        notes=notes_combined,
    )
    return CascadeResult(
        success=ok,
        reason=reason if ok else f"clone_failed:{reason}",
        target_slug=target_slug,
        target_url=target_url,
        target_families=families,
        target_region=region,
        candidates_scored=len(ranked),
        winner=winner,
        runners_up=ranked[1:6],  # top-5 runners-up for diagnostics
        written_path=path,
        skipped_existing=False,
    ) if not weak else CascadeResult(
        success=ok,
        reason="weak_match" if ok else f"clone_failed:{reason}",
        target_slug=target_slug,
        target_url=target_url,
        target_families=families,
        target_region=region,
        candidates_scored=len(ranked),
        winner=winner,
        runners_up=ranked[1:6],
        written_path=path,
        skipped_existing=False,
    )


# ---------------------------------------------------------------------------
# Slug → CMS-family hints
# ---------------------------------------------------------------------------
# Small hand-curated lookup so the candidate-side of the family-overlap
# score isn't always zero. Only includes slugs whose CMS we've already
# observed in production (see replit.md per-uni notes). Add more entries
# here as new YAMLs are tuned. Empty list = "unknown / generic" and only
# region + shape signals contribute.
_CANDIDATE_FAMILY_HINTS: dict[str, list[str]] = {
    "federation": ["nextjs"],
    "torrens":    ["nextjs"],
    "uow":        ["drupal"],
    "une":        ["drupal"],
    "csu":        ["drupal"],
    "deakin":     ["drupal"],
    "monash":     ["drupal"],
    "rmit":       ["drupal"],
    "qut":        ["drupal"],
    "uts":        ["drupal"],
    "unimelb":    ["drupal"],
    "usyd":       ["wagtail"],
    "anu":        ["wagtail"],
    "unsw":       ["drupal"],
    "uq":         ["drupal"],
    "ecu":        ["sitecore"],
    "curtin":     ["sitecore"],
    "macquarie":  ["sitecore"],
    "newcastle":  ["sitecore"],
    "griffith":   ["sitecore"],
    "adelaide":   ["wordpress"],
    "flinders":   ["adobe_aem"],
    "jcu":        ["wordpress"],
    "scu":        ["wordpress"],
    "murdoch":    ["sitecore"],
    "utas":       ["sitecore"],
    "cdu":        ["wordpress"],
    "unisq":      ["sitecore"],
    "latrobe":    ["sitecore"],
    "acu":        ["sitecore"],
    "bond":       ["wordpress"],
    "aut":        ["sitecore"],
}
