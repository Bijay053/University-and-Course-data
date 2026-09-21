"""Auto Repair Candidate Generator + Simulator.

Given a failed scrape job, generates multiple possible fix candidates,
simulates each against historical course URLs, ranks them by
expected improvement × confidence, and returns a ranked list.

Problem classes handled:
  url_filter_drop  — raw_discovered > 0, after_filter == 0  (100% URL drop)
  partial_filter   — raw_discovered > 0, after_filter < raw * 0.5
  low_discovery    — raw_discovered == 0 (JS site / wrong seed URL)
  low_count        — some courses found but far fewer than expected
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ─── Media / asset URL filter ─────────────────────────────────────────────────
# URLs matching these patterns are static assets, not course pages, and must
# never drive allow_url_patterns derivation or appear in "rescued" URL lists.

_MEDIA_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|pdf|css|js|woff2?|ttf|eot|mp[34]|zip|docx?|xlsx?|pptx?)$",
    re.IGNORECASE,
)
_ASSET_PATH_RE = re.compile(
    r"/(images?|assets?|globalassets|static|media|uploads?|files?|fonts?|icons?|styles?|scripts?)/",
    re.IGNORECASE,
)

_FILTER_CONFIG_KEYS = (
    "allow_url_patterns",
    "must_contain",
    "block_url_patterns",
    "course_detail_url_patterns",
)


def normalized_filter_config(config: dict | None) -> dict[str, list[str]]:
    """Return the canonical filter-only snapshot used for CAS comparisons."""
    source = config or {}
    def _values(value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    return {
        key: [str(value) for value in _values(source.get(key)) if value is not None]
        for key in _FILTER_CONFIG_KEYS
    }


def filter_config_fingerprint(config: dict | None) -> str:
    """Return a stable SHA-256 fingerprint for a canonical filter snapshot."""
    payload = json.dumps(
        normalized_filter_config(config),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def filter_config_drifted(
    run_filter_config: dict | None,
    current_filter_config: dict | None,
) -> bool:
    """Return whether the live filter config differs from the config that ran."""
    if not run_filter_config:
        return False
    run = normalized_filter_config(run_filter_config)
    current = normalized_filter_config(current_filter_config)
    return any(
        run[key] != current[key]
        for key in _FILTER_CONFIG_KEYS
    )


def filter_repair_safety_issue(
    run_filter_config: dict | None,
    current_filter_config: dict | None,
    *,
    snapshot_present: bool | None = None,
) -> str | None:
    """Explain why stale run-time rules must not produce clear-filter recipes."""
    if snapshot_present is False or (snapshot_present is None and not run_filter_config):
        return (
            "This job has no run-start URL-filter snapshot. No filter-clearing "
            "recommendation is safe for a legacy job without compare-and-swap evidence."
        )
    if snapshot_present is True:
        has_drift = normalized_filter_config(run_filter_config) != normalized_filter_config(
            current_filter_config
        )
    else:
        has_drift = filter_config_drifted(run_filter_config, current_filter_config)
    if not has_drift:
        return None
    return (
        "The job used a different URL-filter config than the current live config. "
        "No filter-clearing recommendation is safe until the operator confirms "
        "which run-start rules should be restored."
    )


def strip_stale_filter_suggestions(
    suggested_config: dict,
    safety_issue: str | None,
) -> dict:
    """Remove URL-filter recipes from AI output when the run snapshot is stale."""
    if not safety_issue or not isinstance(suggested_config, dict):
        return suggested_config
    discovery = suggested_config.get("discovery")
    if not isinstance(discovery, dict):
        return suggested_config
    filter_keys = set(_FILTER_CONFIG_KEYS)
    safe_discovery = {
        key: value for key, value in discovery.items() if key not in filter_keys
    }
    sanitized = dict(suggested_config)
    if safe_discovery:
        sanitized["discovery"] = safe_discovery
    else:
        sanitized.pop("discovery", None)
    return sanitized


def is_intentionally_excluded_course_url(url: str) -> bool:
    """Recognize proven delivery variants, not generic 'online' keywords.

    Law's catalogue publishes a separate /online/ child of a campus course.
    Its verified recipe deliberately excludes those children. Do not extrapolate
    this evidence to unrelated hosts, campus parent URLs, or unsampled drops.
    This is diagnostic classification only; no runtime filter is relaxed.
    """
    parsed = urlparse(url)
    return (
        parsed.hostname in {"law.ac.uk", "www.law.ac.uk"}
        and re.fullmatch(
            r"/study/(?:undergraduate|postgraduate)/[^/]+/[^/]+/online/?",
            parsed.path, re.IGNORECASE,
        ) is not None
    )


def _is_course_url(url: str) -> bool:
    """Return False for URLs that are clearly media, asset, or static-file paths."""
    path = urlparse(url).path
    if _MEDIA_EXT_RE.search(path):
        return False
    if _ASSET_PATH_RE.search(path):
        return False
    if is_intentionally_excluded_course_url(url):
        return False
    return True


# ─── URL pattern derivation helpers ──────────────────────────────────────────

def _derive_allow_patterns_from_urls(urls: list[str]) -> list[str]:
    """Derive allow_url_patterns from a list of sample dropped/course URLs.

    Analyses path structure to find fixed vs variable segments, then builds a
    regex that matches the variable last segment while fixing the common prefix.

    Example
    -------
    ['https://uni.edu/study/undergrad/courses/python-101',
     'https://uni.edu/study/postgrad/courses/data-science']
    → ['/study/[^/]+/courses/[^/]+/?$']
    """
    if not urls:
        return []
    paths = [urlparse(u).path.rstrip("/") for u in urls if u]
    parts_list = [p.lstrip("/").split("/") for p in paths if p]
    if not parts_list:
        return []

    max_depth = max(len(p) for p in parts_list)
    min_depth = min(len(p) for p in parts_list)

    pattern_parts: list[str] = []
    for i in range(max_depth):
        values = {p[i] for p in parts_list if i < len(p)}
        if len(values) == 1 and i < min_depth:
            pattern_parts.append(re.escape(list(values)[0]))
        else:
            pattern_parts.append(r"[^/]+")

    if not pattern_parts:
        return []

    return ["/" + "/".join(pattern_parts) + r"/?$"]


def _derive_seed_urls(dropped_urls: list[str], fallback_url: str = "") -> list[str]:
    """Extract distinct parent-directory seed URLs from dropped course URLs.

    Takes the path up to the LAST variable segment (the course slug) and
    returns unique directories with the scheme+host prepended.
    """
    if not dropped_urls:
        return [fallback_url] if fallback_url else []

    parts_list = []
    host = ""
    for u in dropped_urls:
        parsed = urlparse(u)
        if not host:
            host = f"{parsed.scheme}://{parsed.netloc}"
        segs = parsed.path.rstrip("/").lstrip("/").split("/")
        parts_list.append(segs)

    if not parts_list:
        return [fallback_url] if fallback_url else []

    # Find depth of last fixed segment (everything above the course slug)
    max_depth = max(len(p) for p in parts_list)
    min_depth = min(len(p) for p in parts_list)

    # Walk from the end: the last segment is the course slug (always variable)
    # Find the deepest segment that has variation — that's where seeds go
    seed_depth = max(1, min_depth - 1)  # parent of last segment
    for i in range(max_depth - 1, 0, -1):
        values = {p[i] for p in parts_list if i < len(p)}
        if len(values) > 1:
            seed_depth = i  # variable segment — seed its parent
            break

    # Build unique seed paths at seed_depth
    seen: set[str] = set()
    seeds: list[str] = []
    for parts in parts_list:
        path = "/" + "/".join(parts[:seed_depth]) + "/"
        full = host + path
        if full not in seen:
            seen.add(full)
            seeds.append(full)
    return seeds[:6] or ([fallback_url] if fallback_url else [])


_STANDARD_BLOCK_PATTERNS = [
    r"/apply",
    r"/contact",
    r"/news",
    r"/events",
    r"/blog",
    r"/about",
    r"/research",
    r"/alumni",
    r"/outreach",
    r"/parents",
    r"/jobs",
    r"/careers",
    r"/accommodation",
    r"/life-on-campus",
    r"/fees-and-funding$",
    r"/international/living",
]


def _build_proposed_yaml(
    seed_urls: list[str],
    allow_pats: list[str],
    block_pats: list[str],
) -> str:
    """Render a human-readable YAML snippet for the proposed config fix."""
    lines = ["discovery:"]
    if seed_urls:
        lines.append("  seed_urls:")
        for u in seed_urls:
            lines.append(f"    - {u}")
        lines.append("")
    if allow_pats:
        lines.append("  allow_url_patterns:")
        for p in allow_pats:
            lines.append(f"    - '{p}'")
        lines.append("")
    if block_pats:
        lines.append("  block_url_patterns:")
        for p in block_pats:
            lines.append(f"    - '{p}'")
    return "\n".join(lines)

# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class SimulationResult:
    method: str                            # "historical_filter" | "job_stats" | "estimated"
    before_count: int                      # URLs/courses before fix
    after_count: int                       # URLs/courses after fix
    drop_rate_before_pct: int
    drop_rate_after_pct: int
    historical_url_count: int              # how many historical URLs used for simulation
    sample_urls_rescued: list[str] = field(default_factory=list)
    sample_urls_kept: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class RepairCandidate:
    id: str                    # "clear_allow_patterns", "enable_browser", etc.
    rank: int                  # 1 = top / recommended
    label: str                 # short display label
    description: str           # what this fix does and why
    category: str              # "url_filter" | "discovery" | "url_rewrite" | "extraction"
    problem_addressed: str     # brief problem statement
    recipe_patch: dict         # the admin_config patch to apply
    simulation: SimulationResult
    confidence: int            # 0-100
    is_recommended: bool = False
    safety_gate_passed: bool = False
    expected_gain: int = 0     # after_count - before_count
    selection_reason: str = "" # human-readable explanation of why this fix was chosen / ranked here
    proposed_yaml: str | None = None  # full YAML snippet for display in the UI

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ─── Engine ───────────────────────────────────────────────────────────────────

class AutoRepairEngine:
    """Generates and ranks repair candidates for a failed scrape job."""

    def __init__(
        self,
        *,
        uni_id: int,
        uni_name: str,
        scrape_url: str,
        current_allow_pats: list[str],
        current_must_contain: list[str],
        current_block_pats: list[str],
        raw_discovered: int,
        after_filter: int,
        imported: int,
        historical_urls: list[str],
        pipeline_stats: dict,
        current_course_detail_pats: list[str] | None = None,
        dropped_sample: list[str] | None = None,
    ):
        self.uni_id = uni_id
        self.uni_name = uni_name
        self.scrape_url = scrape_url
        self.allow_pats = current_allow_pats
        self.must_contain = current_must_contain
        self.block_pats = current_block_pats
        self.course_detail_pats = current_course_detail_pats or []
        self.raw_discovered = raw_discovered
        self.after_filter = after_filter
        self.imported = imported
        self.historical_urls = [url for url in historical_urls if _is_course_url(url)]
        self.pipeline_stats = pipeline_stats
        self.dropped_sample: list[str] = [url for url in (dropped_sample or []) if _is_course_url(url)]
        self.only_intentional_drop_evidence = bool(dropped_sample) and all(
            is_intentionally_excluded_course_url(url) for url in dropped_sample
        )
        # Block-filter stats captured from inside discover_course_links.
        # pre_block_discovered = raw count BEFORE block_url_patterns ran.
        # block_dropped_count   = how many URLs were removed by block patterns.
        self.pre_block_discovered: int = pipeline_stats.get("pre_block_discovered", raw_discovered)
        self.block_dropped_count: int = pipeline_stats.get("block_dropped_count", 0)

    # ── Problem classification ─────────────────────────────────────────────────

    def _classify_problem(self) -> str:
        # Block patterns are the primary culprit when they drop >80% of all
        # URLs discovered before the block filter ran.  This is a separate
        # case from "url_filter_drop" because the raw count visible to the
        # allow/must filters is already tiny — the real problem is upstream.
        if (
            self.pre_block_discovered > 5
            and self.block_dropped_count > 0
            and self.block_dropped_count > self.pre_block_discovered * 0.80
        ):
            return "block_catastrophic"
        if self.raw_discovered > 0 and self.after_filter == 0:
            return "url_filter_drop"
        if self.raw_discovered > 0 and self.after_filter < self.raw_discovered * 0.5:
            return "partial_filter"
        if self.raw_discovered == 0:
            return "low_discovery"
        if self.imported > 0 and self.imported < 30:
            return "low_count"
        return "unknown"

    def _filter_funnel_str(self) -> str:
        """Human-readable filter funnel: 128 raw → block dropped 124 → 4 → allow dropped 4 → 0."""
        if self.block_dropped_count <= 0:
            return ""
        pct = self.pipeline_stats.get("block_dropped_pct", 0)
        allow_mc_dropped = self.raw_discovered - self.after_filter
        return (
            f"{self.pre_block_discovered} discovered → "
            f"block dropped {self.block_dropped_count} ({pct}%) → "
            f"{self.raw_discovered} remain → "
            f"allow/must dropped {allow_mc_dropped} → "
            f"{self.after_filter} extractable"
        )

    # ── Core filter simulator ──────────────────────────────────────────────────

    def _simulate_filter(
        self,
        allow_pats: list[str],
        must_contain: list[str],
        block_pats: list[str],
        course_detail_pats: list[str] | None = None,
        *,
        sample_urls: list[str] | None = None,
    ) -> SimulationResult:
        """Apply a candidate filter config to historical URLs and measure the effect."""
        urls = self.historical_urls if sample_urls is None else sample_urls
        hist_count = len(urls)
        total_raw = self.raw_discovered

        # ── No historical data — estimate from job stats ─────────────────────
        if not urls:
            has_any_filter = bool(
                allow_pats or must_contain or block_pats or course_detail_pats
            )
            if not has_any_filter:
                # Clearing all filters → all raw_discovered URLs would pass
                return SimulationResult(
                    method="job_stats",
                    before_count=self.after_filter,
                    after_count=total_raw,
                    drop_rate_before_pct=100 if total_raw > 0 else 0,
                    drop_rate_after_pct=0,
                    historical_url_count=0,
                    note=(
                        f"Estimated: {total_raw} URLs discovered by the last scrape "
                        f"would all pass with no filter."
                    ),
                )
            return SimulationResult(
                method="estimated",
                before_count=self.after_filter,
                after_count=max(1, int(total_raw * 0.65)),
                drop_rate_before_pct=100 if total_raw > 0 and self.after_filter == 0 else 50,
                drop_rate_after_pct=35,
                historical_url_count=0,
                note="Estimated: no historical URL data — using conservative 65% pass-through estimate.",
            )

        # ── Compile patterns ────────────────────────────────────────────────
        def _compile(pats: list[str]) -> list[re.Pattern]:
            return [re.compile(p, re.IGNORECASE) for p in pats if p]

        cur_allow = _compile(self.allow_pats)
        cur_block = _compile(self.block_pats)
        cur_detail = _compile(self.course_detail_pats)
        cur_mc = [m.lower() for m in self.must_contain if m]

        new_allow = _compile(allow_pats)
        new_block = _compile(block_pats)
        new_detail = _compile(
            self.course_detail_pats if course_detail_pats is None else course_detail_pats
        )
        new_mc = [m.lower() for m in must_contain if m]

        def _passes(url: str, a_pats, mc, b_pats, detail_pats) -> bool:
            ul = url.lower()
            if a_pats and not any(p.search(url) for p in a_pats):
                return False
            if mc and not any(m in ul for m in mc):
                return False
            if b_pats and any(p.search(url) for p in b_pats):
                return False
            if detail_pats and not any(p.search(url) for p in detail_pats):
                return False
            return True

        before_passing = [
            u for u in urls if _passes(u, cur_allow, cur_mc, cur_block, cur_detail)
        ]
        before_dropped = [
            u for u in urls if not _passes(u, cur_allow, cur_mc, cur_block, cur_detail)
        ]
        after_passing = [
            u for u in urls if _passes(u, new_allow, new_mc, new_block, new_detail)
        ]
        after_dropped = [
            u for u in urls if not _passes(u, new_allow, new_mc, new_block, new_detail)
        ]

        rescued = [u for u in after_passing if u in set(before_dropped)]

        before_drop_pct = round(len(before_dropped) / hist_count * 100) if hist_count else 0
        after_drop_pct = round(len(after_dropped) / hist_count * 100) if hist_count else 0

        return SimulationResult(
            method="historical_filter",
            before_count=len(before_passing),
            after_count=len(after_passing),
            drop_rate_before_pct=before_drop_pct,
            drop_rate_after_pct=after_drop_pct,
            historical_url_count=hist_count,
            sample_urls_rescued=rescued[:5],
            sample_urls_kept=after_passing[:5],
        )

    # ── Pattern derivation helpers ─────────────────────────────────────────────

    def _derive_relaxed_pattern(self) -> list[str]:
        """Strip degree-type alternation from allow_url_patterns to produce a looser version."""
        relaxed = []
        for pat in self.allow_pats:
            p = pat.rstrip("$")
            # Replace non-capturing alternation groups (?:bachelor|master|...) with [^/]+
            p = re.sub(r'\(\?:[^)]+\)', '[^/]+', p)
            # Replace bare alternation groups (a|b|c) with [^/]+
            p = re.sub(r'\([^)]+\|[^)]+\)', '[^/]+', p)
            # Ensure trailing slash
            p = p.rstrip("*").rstrip("/") + "/"
            if p != pat:
                relaxed.append(p)
        return relaxed or self.allow_pats

    # ── Candidate generators ───────────────────────────────────────────────────

    def _url_filter_candidates(self) -> list[RepairCandidate]:
        if self.only_intentional_drop_evidence:
            # A truncated sample cannot certify the unsampled URLs as missing
            # courses. Never turn its raw drop count into an estimated rescue.
            return []
        candidates: list[RepairCandidate] = []
        total_raw = self.raw_discovered
        has_hist = len(self.historical_urls) >= 5

        def _conf_base(no_hist_val: int) -> int:
            return 90 if has_hist else no_hist_val

        # Fix A — Remove ALL filters
        if (
            self.allow_pats
            or self.must_contain
            or self.block_pats
            or self.course_detail_pats
        ):
            sim = self._simulate_filter([], [], [], [])
            gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
            candidates.append(RepairCandidate(
                id="clear_all_filters",
                rank=0,
                label="Remove all URL filters",
                description=(
                    "Clears allow_url_patterns, must_contain, block_url_patterns, and "
                    "course_detail_url_patterns. "
                    "The scraper will accept every discovered link. "
                    "Use this to confirm discovery is working, then re-add targeted filters."
                ),
                category="url_filter",
                problem_addressed="All discovered URLs blocked by filter",
                recipe_patch={"discovery": {
                    "allow_url_patterns": [],
                    "must_contain": [],
                    "block_url_patterns": [],
                    "course_detail_url_patterns": [],
                }},
                simulation=sim,
                confidence=_conf_base(88) if total_raw > 0 else 50,
                safety_gate_passed=gate,
                expected_gain=max(0, sim.after_count - sim.before_count),
            ))

        # Fix B — Remove only allow_url_patterns
        if self.allow_pats:
            sim = self._simulate_filter([], self.must_contain, self.block_pats)
            gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
            label_pats = ", ".join(f'"{p}"' for p in self.allow_pats[:2])
            if len(self.allow_pats) > 2:
                label_pats += f" +{len(self.allow_pats) - 2} more"
            candidates.append(RepairCandidate(
                id="clear_allow_patterns",
                rank=0,
                label="Remove allow_url_patterns",
                description=(
                    f"Clears the allow pattern(s): {label_pats}. "
                    "This filter was requiring URLs to match a specific regex. "
                    "must_contain and block_url_patterns are kept unchanged."
                ),
                category="url_filter",
                problem_addressed="allow_url_patterns regex too restrictive",
                recipe_patch={"discovery": {"allow_url_patterns": []}},
                simulation=sim,
                confidence=_conf_base(83) if total_raw > 0 else 45,
                safety_gate_passed=gate,
                expected_gain=max(0, sim.after_count - sim.before_count),
            ))

        # Fix C — Remove only must_contain
        if self.must_contain:
            sim = self._simulate_filter(self.allow_pats, [], self.block_pats)
            gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
            label_mc = ", ".join(f'"{m}"' for m in self.must_contain[:2])
            candidates.append(RepairCandidate(
                id="clear_must_contain",
                rank=0,
                label="Remove must_contain filter",
                description=(
                    f"Clears must_contain: {label_mc}. "
                    "This filter required a specific substring in every course URL. "
                    "allow_url_patterns and block_url_patterns are kept."
                ),
                category="url_filter",
                problem_addressed="must_contain substring not found in course URLs",
                recipe_patch={"discovery": {"must_contain": []}},
                simulation=sim,
                confidence=_conf_base(85) if total_raw > 0 else 40,
                safety_gate_passed=gate,
                expected_gain=max(0, sim.after_count - sim.before_count),
            ))

        # Fix D — Remove only block_url_patterns
        if self.block_pats:
            sim = self._simulate_filter(self.allow_pats, self.must_contain, [])
            gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
            label_bp = ", ".join(f'"{p}"' for p in self.block_pats[:2])
            if len(self.block_pats) > 2:
                label_bp += f" +{len(self.block_pats) - 2} more"
            _block_catast = (
                self.block_dropped_count > 0
                and self.pre_block_discovered > 5
                and self.block_dropped_count > self.pre_block_discovered * 0.80
            )
            _block_pct = self.pipeline_stats.get("block_dropped_pct", 0)
            _funnel = self._filter_funnel_str()
            candidates.append(RepairCandidate(
                id="clear_block_patterns",
                rank=0,
                label=(
                    "Remove block_url_patterns — primary fix (over-blocking course URLs)"
                    if _block_catast
                    else "Remove block_url_patterns"
                ),
                description=(
                    (
                        f"⚠ block_url_patterns dropped {self.block_dropped_count} / "
                        f"{self.pre_block_discovered} discovered URLs ({_block_pct}%) — "
                        f"this is the primary problem. "
                        if _block_catast else ""
                    )
                    + f"Clears block pattern(s): {label_bp}. "
                    + (
                        "allow_url_patterns and must_contain are kept."
                        + (f" Filter funnel: {_funnel}." if _funnel else "")
                    )
                ),
                category="url_filter",
                problem_addressed=(
                    f"block_url_patterns dropped {self.block_dropped_count}/{self.pre_block_discovered} "
                    f"discovered URLs ({_block_pct}%) — catastrophic over-blocking"
                    if _block_catast
                    else "block_url_patterns accidentally matching course URLs"
                ),
                recipe_patch={"discovery": {"block_url_patterns": []}},
                simulation=sim,
                confidence=(
                    _conf_base(93) if _block_catast
                    else _conf_base(78) if total_raw > 0
                    else 35
                ),
                safety_gate_passed=gate,
                expected_gain=max(0, sim.after_count - sim.before_count),
            ))

        # Fix E — Remove only the final course-detail URL gate.
        if self.course_detail_pats:
            sim = self._simulate_filter(
                self.allow_pats,
                self.must_contain,
                self.block_pats,
                [],
            )
            gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
            label_pats = ", ".join(f'"{p}"' for p in self.course_detail_pats[:2])
            if len(self.course_detail_pats) > 2:
                label_pats += f" +{len(self.course_detail_pats) - 2} more"
            candidates.append(RepairCandidate(
                id="clear_course_detail_patterns",
                rank=0,
                label="Remove course_detail_url_patterns",
                description=(
                    f"Clears the final course-detail URL gate: {label_pats}. "
                    "This gate was rejecting URLs before extraction; allow, must_contain, "
                    "and block_url_patterns are kept."
                ),
                category="url_filter",
                problem_addressed="course_detail_url_patterns regex rejects discovered course URLs",
                recipe_patch={"discovery": {"course_detail_url_patterns": []}},
                simulation=sim,
                confidence=_conf_base(85) if total_raw > 0 else 40,
                safety_gate_passed=gate,
                expected_gain=max(0, sim.after_count - sim.before_count),
            ))

        # Fix F — Relax allow_url_patterns (drop alternation, keep path prefix)
        if self.allow_pats:
            relaxed = self._derive_relaxed_pattern()
            if relaxed != self.allow_pats:
                sim = self._simulate_filter(relaxed, self.must_contain, self.block_pats)
                gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
                candidates.append(RepairCandidate(
                    id="relax_allow_patterns",
                    rank=0,
                    label="Relax allow_url_patterns (remove degree alternation)",
                    description=(
                        f'Simplifies "{self.allow_pats[0][:55]}…" → "{relaxed[0][:55]}…". '
                        "Removes strict degree-type alternation groups (bachelor|master|…) "
                        "while keeping the path prefix intact. More permissive but still targeted."
                    ),
                    category="url_filter",
                    problem_addressed="allow_url_patterns alternation group doesn't match actual URL format",
                    recipe_patch={"discovery": {"allow_url_patterns": relaxed}},
                    simulation=sim,
                    confidence=_conf_base(70) if total_raw > 0 else 30,
                    safety_gate_passed=gate,
                    expected_gain=max(0, sim.after_count - sim.before_count),
                ))

        return candidates

    def _discovery_candidates(self, supplement: bool = False) -> list[RepairCandidate]:
        """Candidates for low/zero discovery (JS site, wrong seed, sitemap missing)."""
        prefix = "Supplement: " if supplement else ""
        conf_adj = -20 if supplement else 0
        est_base = max(self.raw_discovered, 30 if not supplement else 20)

        def _est(mult: float) -> int:
            return max(self.raw_discovered, int(est_base * mult))

        def _sim(after: int, note: str) -> SimulationResult:
            return SimulationResult(
                method="estimated",
                before_count=self.raw_discovered,
                after_count=after,
                drop_rate_before_pct=100 if self.raw_discovered == 0 else 0,
                drop_rate_after_pct=0,
                historical_url_count=0,
                note=note,
            )

        return [
            RepairCandidate(
                id="enable_browser_and_sitemap",
                rank=0,
                label=f"{prefix}Browser + sitemap (most thorough)",
                description=(
                    "Enables Playwright browser rendering AND sitemap.xml crawling. "
                    "Best for sites that use JavaScript AND have a sitemap. "
                    "Sitemap handles the bulk discovery; browser catches JS-gated extras."
                ),
                category="discovery",
                problem_addressed="JS-rendered site — HTTP BFS returns 0 or very few links",
                recipe_patch={"discovery": {
                    "always_browser_discover": True,
                    "always_sitemap_supplement": True,
                    "bfs_page_budget": 60,
                }},
                simulation=_sim(
                    _est(2.5),
                    "Estimated: browser + sitemap typically discovers 80-300 course links.",
                ),
                confidence=max(10, 72 + conf_adj),
                safety_gate_passed=True,
                expected_gain=max(0, _est(2.5) - self.raw_discovered),
            ),
            RepairCandidate(
                id="enable_sitemap",
                rank=0,
                label=f"{prefix}Enable sitemap supplement",
                description=(
                    "Adds sitemap.xml crawling alongside BFS. "
                    "Sitemaps often list all course URLs directly, bypassing JS rendering "
                    "and page budget limits. Very fast, low risk."
                ),
                category="discovery",
                problem_addressed="Course URLs not reachable via BFS",
                recipe_patch={"discovery": {"always_sitemap_supplement": True}},
                simulation=_sim(
                    _est(1.8),
                    "Estimated: sitemaps typically list 50-500 course URLs for large universities.",
                ),
                confidence=max(10, 60 + conf_adj),
                safety_gate_passed=True,
                expected_gain=max(0, _est(1.8) - self.raw_discovered),
            ),
            RepairCandidate(
                id="enable_browser_discover",
                rank=0,
                label=f"{prefix}Enable browser-based discovery",
                description=(
                    "Enables Playwright browser discovery which renders JavaScript. "
                    "Required for React/Vue/Angular SPAs where static HTTP crawling "
                    "returns 0 links. Slower but finds JS-rendered course pages."
                ),
                category="discovery",
                problem_addressed="Site renders courses via JavaScript — HTTP BFS gets 0",
                recipe_patch={"discovery": {
                    "always_browser_discover": True,
                    "bfs_page_budget": 60,
                }},
                simulation=_sim(
                    _est(1.5),
                    "Estimated: browser discovery typically recovers 30-150 course links.",
                ),
                confidence=max(10, 62 + conf_adj),
                safety_gate_passed=True,
                expected_gain=max(0, _est(1.5) - self.raw_discovered),
            ),
        ]

    def _low_count_candidates(self) -> list[RepairCandidate]:
        bfs_current = self.pipeline_stats.get("bfs_page_budget", 25)
        return [
            RepairCandidate(
                id="increase_bfs_budget",
                rank=0,
                label=f"Increase BFS page budget ({bfs_current} → 80)",
                description=(
                    f"Raises the BFS page budget from {bfs_current} to 80. "
                    "When the catalogue spans many listing pages (pagination), "
                    "a low budget causes the crawler to stop before visiting all pages. "
                    "Most common cause of partial discovery (e.g. 54 found, 150 expected)."
                ),
                category="discovery",
                problem_addressed="BFS budget exhausted before all listing pages were visited",
                recipe_patch={"discovery": {"bfs_page_budget": 80}},
                simulation=SimulationResult(
                    method="estimated",
                    before_count=self.imported,
                    after_count=min(self.imported * 2, 300),
                    drop_rate_before_pct=0,
                    drop_rate_after_pct=0,
                    historical_url_count=0,
                    note="Estimated: doubling page budget typically doubles course count for paginated catalogues.",
                ),
                confidence=58,
                safety_gate_passed=True,
                expected_gain=self.imported,
            ),
        ] + self._discovery_candidates(supplement=True)

    # ── Smart YAML candidate (from dropped URL analysis) ───────────────────────

    def _smart_replace_allow_patterns(self) -> list[RepairCandidate]:
        """Derive replacement allow_url_patterns from the actual dropped URL sample.

        This produces a POSITIVE fix — not just "clear the broken pattern" but
        "here is a new pattern derived from the URLs the filter is dropping."
        Only generated when we have at least 3 dropped-URL samples.
        """
        if len(self.dropped_sample) < 3:
            return []

        # Strip media/asset URLs — they must never drive allow_url_patterns
        # derivation (e.g. .jpg images from /globalassets/ should not produce
        # a pattern that matches the entire site).
        course_urls = [u for u in self.dropped_sample if _is_course_url(u)]
        if len(course_urls) < 3:
            return []

        new_pats = _derive_allow_patterns_from_urls(course_urls)
        if not new_pats:
            return []

        seed_urls = _derive_seed_urls(course_urls, self.scrape_url)

        # Smart block patterns: use the standard set minus any that would
        # accidentally block the paths we just allowed.
        block_pats = [
            b for b in _STANDARD_BLOCK_PATTERNS
            if not any(re.search(b, p, re.IGNORECASE) for p in new_pats)
        ]

        # Simulate the exact merged resulting filter state. Replacing only the
        # allowlist must not accidentally make the candidate look safe by
        # clearing the existing must_contain/block/detail gates.
        proposed_block_pats = block_pats
        sim = self._simulate_filter(
            new_pats,
            self.must_contain,
            proposed_block_pats,
            self.course_detail_pats,
        )

        # For a brand-new uni with no historical URLs, at least we know the
        # dropped_sample itself should now pass. Compare the exact current and
        # proposed merged configs against that sample.
        if not self.historical_urls:
            baseline = self._simulate_filter(
                self.allow_pats,
                self.must_contain,
                self.block_pats,
                self.course_detail_pats,
                sample_urls=course_urls,
            )
            sim = self._simulate_filter(
                new_pats,
                self.must_contain,
                proposed_block_pats,
                self.course_detail_pats,
                sample_urls=course_urls,
            )
            sim.method = "dropped_sample_filter"
            sim.note = (
                f"Simulated the merged allow/must/block/detail config against "
                f"{len(course_urls)} dropped-URL sample(s): "
                f"{baseline.after_count} before → {sim.after_count} after."
            )

        gate = sim.after_count > 0
        proposed_yaml = _build_proposed_yaml(seed_urls, new_pats, block_pats)

        return [RepairCandidate(
            id="smart_replace_patterns",
            rank=0,
            label="Replace allow_url_patterns with patterns derived from dropped URLs",
            description=(
                f"Derived {len(new_pats)} pattern(s) from {len(course_urls)} "
                f"course URL sample(s). Replaces the broken filter with one that "
                f"matches actual course pages."
            ),
            category="url_filter",
            problem_addressed=(
                f"Filter funnel: {self._filter_funnel_str()}"
                if self._filter_funnel_str()
                else f"Current allow_url_patterns match 0 of {self.raw_discovered} discovered URLs"
            ),
            recipe_patch={"discovery": {
                "allow_url_patterns": new_pats,
                "block_url_patterns": block_pats,
            }},
            simulation=sim,
            confidence=80 if len(course_urls) >= 6 else 60,
            safety_gate_passed=gate,
            expected_gain=max(0, sim.after_count - sim.before_count),
            proposed_yaml=proposed_yaml,
        )]

    # ── Main entry point ───────────────────────────────────────────────────────

    def generate_candidates(self) -> list[RepairCandidate]:
        problem = self._classify_problem()
        log.info(
            "AutoRepair: uni=%s problem=%s raw=%d after=%d hist=%d",
            self.uni_name, problem, self.raw_discovered, self.after_filter,
            len(self.historical_urls),
        )

        candidates: list[RepairCandidate] = []

        if problem == "block_catastrophic":
            # block_url_patterns dropped >80% of all discovered URLs.
            # Generate all filter candidates — Fix D (clear_block_patterns)
            # will rank #1 due to elevated confidence (93%) and after_count.
            candidates.extend(self._url_filter_candidates())
            candidates.extend(self._smart_replace_allow_patterns())
            if self.raw_discovered < 15:
                candidates.extend(self._discovery_candidates(supplement=True))
        elif problem in ("url_filter_drop", "partial_filter"):
            candidates.extend(self._url_filter_candidates())
            # Smart positive fix: derived from the actual dropped URL sample.
            # Inserted before the generic "clear everything" candidates so it
            # surfaces first if the pattern matches well.
            candidates.extend(self._smart_replace_allow_patterns())
            # Also offer discovery fixes if raw_discovered is itself low
            if self.raw_discovered < 15:
                candidates.extend(self._discovery_candidates(supplement=True))
        elif problem == "low_discovery":
            candidates.extend(self._discovery_candidates())
        elif problem == "low_count":
            candidates.extend(self._low_count_candidates())
        else:
            # Unknown — offer both discovery and filter options
            candidates.extend(self._url_filter_candidates())
            candidates.extend(self._discovery_candidates(supplement=True))

        # Sort: safety gate first, then after_count desc, then confidence desc
        candidates.sort(key=lambda c: (
            -int(c.safety_gate_passed),
            -(c.simulation.after_count),
            -(c.confidence),
        ))

        # Deduplicate by id (keep first / highest-ranked)
        seen: set[str] = set()
        unique: list[RepairCandidate] = []
        for c in candidates:
            if c.id not in seen:
                seen.add(c.id)
                unique.append(c)

        # Assign ranks, mark recommended, and populate selection_reason
        for i, c in enumerate(unique):
            c.rank = i + 1
            c.is_recommended = i == 0 and c.safety_gate_passed
            if not c.safety_gate_passed:
                c.selection_reason = (
                    f"Safety gate failed: only {c.simulation.after_count} URLs would survive "
                    f"with a {c.simulation.drop_rate_after_pct}% drop rate. "
                    f"Applying this fix could cause the scraper to miss most courses."
                )
            elif i == 0:
                c.selection_reason = (
                    f"Ranked #1 because it rescues the most URLs "
                    f"({c.simulation.after_count} from {c.simulation.before_count}) "
                    f"at {c.confidence}% confidence, keeping the drop rate at "
                    f"{c.simulation.drop_rate_after_pct}%. "
                    f"Simulation method: {c.simulation.method.replace('_', ' ')}."
                )
            else:
                c.selection_reason = (
                    f"Ranked #{c.rank}: rescues {c.simulation.after_count} URLs "
                    f"at {c.confidence}% confidence — fewer than the recommended fix."
                )

        return unique


# ─── Public API ───────────────────────────────────────────────────────────────

async def generate_repair_candidates(
    *,
    uni_id: int,
    uni_name: str,
    scrape_url: str,
    current_allow_pats: list[str],
    current_must_contain: list[str],
    current_block_pats: list[str],
    raw_discovered: int,
    after_filter: int,
    imported: int,
    historical_urls: list[str],
    pipeline_stats: dict,
    current_course_detail_pats: list[str] | None = None,
    dropped_sample: list[str] | None = None,
) -> list[dict]:
    """Async entry point — returns ranked candidate dicts ready for JSON serialisation."""
    engine = AutoRepairEngine(
        uni_id=uni_id,
        uni_name=uni_name,
        scrape_url=scrape_url,
        current_allow_pats=current_allow_pats,
        current_must_contain=current_must_contain,
        current_block_pats=current_block_pats,
        current_course_detail_pats=current_course_detail_pats,
        raw_discovered=raw_discovered,
        after_filter=after_filter,
        imported=imported,
        historical_urls=historical_urls,
        pipeline_stats=pipeline_stats,
        dropped_sample=dropped_sample,
    )
    return [c.to_dict() for c in engine.generate_candidates()]
