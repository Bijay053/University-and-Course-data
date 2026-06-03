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

import re
import logging
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

log = logging.getLogger(__name__)


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
        dropped_sample: list[str] | None = None,
    ):
        self.uni_id = uni_id
        self.uni_name = uni_name
        self.scrape_url = scrape_url
        self.allow_pats = current_allow_pats
        self.must_contain = current_must_contain
        self.block_pats = current_block_pats
        self.raw_discovered = raw_discovered
        self.after_filter = after_filter
        self.imported = imported
        self.historical_urls = historical_urls
        self.pipeline_stats = pipeline_stats
        self.dropped_sample: list[str] = dropped_sample or []

    # ── Problem classification ─────────────────────────────────────────────────

    def _classify_problem(self) -> str:
        if self.raw_discovered > 5 and self.after_filter == 0:
            return "url_filter_drop"
        if self.raw_discovered > 5 and self.after_filter < self.raw_discovered * 0.5:
            return "partial_filter"
        if self.raw_discovered == 0:
            return "low_discovery"
        if self.imported > 0 and self.imported < 30:
            return "low_count"
        return "unknown"

    # ── Core filter simulator ──────────────────────────────────────────────────

    def _simulate_filter(
        self,
        allow_pats: list[str],
        must_contain: list[str],
        block_pats: list[str],
    ) -> SimulationResult:
        """Apply a candidate filter config to historical URLs and measure the effect."""
        urls = self.historical_urls
        hist_count = len(urls)
        total_raw = self.raw_discovered

        # ── No historical data — estimate from job stats ─────────────────────
        if not urls:
            has_any_filter = bool(allow_pats or must_contain or block_pats)
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
        cur_mc = [m.lower() for m in self.must_contain if m]

        new_allow = _compile(allow_pats)
        new_block = _compile(block_pats)
        new_mc = [m.lower() for m in must_contain if m]

        def _passes(url: str, a_pats, mc, b_pats) -> bool:
            ul = url.lower()
            if a_pats and not any(p.search(url) for p in a_pats):
                return False
            if mc and not any(m in ul for m in mc):
                return False
            if b_pats and any(p.search(url) for p in b_pats):
                return False
            return True

        before_passing = [u for u in urls if _passes(u, cur_allow, cur_mc, cur_block)]
        before_dropped = [u for u in urls if not _passes(u, cur_allow, cur_mc, cur_block)]
        after_passing = [u for u in urls if _passes(u, new_allow, new_mc, new_block)]
        after_dropped = [u for u in urls if not _passes(u, new_allow, new_mc, new_block)]

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
        candidates: list[RepairCandidate] = []
        total_raw = self.raw_discovered
        has_hist = len(self.historical_urls) >= 5

        def _conf_base(no_hist_val: int) -> int:
            return 90 if has_hist else no_hist_val

        # Fix A — Remove ALL filters
        if self.allow_pats or self.must_contain or self.block_pats:
            sim = self._simulate_filter([], [], [])
            gate = sim.after_count > 0 and sim.drop_rate_after_pct < 70
            candidates.append(RepairCandidate(
                id="clear_all_filters",
                rank=0,
                label="Remove all URL filters",
                description=(
                    "Clears allow_url_patterns, must_contain, and block_url_patterns. "
                    "The scraper will accept every discovered link. "
                    "Use this to confirm discovery is working, then re-add targeted filters."
                ),
                category="url_filter",
                problem_addressed="All discovered URLs blocked by filter",
                recipe_patch={"discovery": {
                    "allow_url_patterns": [],
                    "must_contain": [],
                    "block_url_patterns": [],
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
            candidates.append(RepairCandidate(
                id="clear_block_patterns",
                rank=0,
                label="Remove block_url_patterns",
                description=(
                    f"Clears block pattern(s): {label_bp}. "
                    "These patterns were blocking URLs that happened to match. "
                    "allow_url_patterns and must_contain are kept."
                ),
                category="url_filter",
                problem_addressed="block_url_patterns accidentally matching course URLs",
                recipe_patch={"discovery": {"block_url_patterns": []}},
                simulation=sim,
                confidence=_conf_base(78) if total_raw > 0 else 35,
                safety_gate_passed=gate,
                expected_gain=max(0, sim.after_count - sim.before_count),
            ))

        # Fix E — Relax allow_url_patterns (drop alternation, keep path prefix)
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

        new_pats = _derive_allow_patterns_from_urls(self.dropped_sample)
        if not new_pats:
            return []

        seed_urls = _derive_seed_urls(self.dropped_sample, self.scrape_url)

        # Smart block patterns: use the standard set minus any that would
        # accidentally block the paths we just allowed.
        block_pats = [
            b for b in _STANDARD_BLOCK_PATTERNS
            if not any(re.search(b, p, re.IGNORECASE) for p in new_pats)
        ]

        # Simulate the new filter against historical URLs (or estimate from raw)
        sim = self._simulate_filter(new_pats, [], [])

        # For a brand-new uni with no historical URLs, at least we know the
        # dropped_sample itself should now pass — use that as the before/after.
        if not self.historical_urls:
            compiled = [re.compile(p, re.IGNORECASE) for p in new_pats if p]
            rescued = [u for u in self.dropped_sample if compiled and any(c.search(u) for c in compiled)]
            sim = SimulationResult(
                method="dropped_sample_filter",
                before_count=0,
                after_count=len(rescued),
                drop_rate_before_pct=100,
                drop_rate_after_pct=0 if rescued else 100,
                historical_url_count=len(self.dropped_sample),
                sample_urls_rescued=rescued[:5],
                note=(
                    f"Simulated against {len(self.dropped_sample)} dropped-URL sample(s). "
                    f"{len(rescued)} would pass the new pattern."
                ),
            )

        gate = sim.after_count > 0
        proposed_yaml = _build_proposed_yaml(seed_urls, new_pats, block_pats)

        return [RepairCandidate(
            id="smart_replace_patterns",
            rank=0,
            label="Replace allow_url_patterns with patterns derived from dropped URLs",
            description=(
                f"Derived {len(new_pats)} pattern(s) from {len(self.dropped_sample)} "
                f"dropped URL sample(s). Replaces the broken filter with one that "
                f"matches actual course pages."
            ),
            category="url_filter",
            problem_addressed=(
                f"Current allow_url_patterns match 0 of {self.raw_discovered} discovered URLs"
            ),
            recipe_patch={"discovery": {
                "allow_url_patterns": new_pats,
                "block_url_patterns": block_pats,
            }},
            simulation=sim,
            confidence=80 if len(self.dropped_sample) >= 6 else 60,
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

        if problem in ("url_filter_drop", "partial_filter"):
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
        raw_discovered=raw_discovered,
        after_filter=after_filter,
        imported=imported,
        historical_urls=historical_urls,
        pipeline_stats=pipeline_stats,
        dropped_sample=dropped_sample,
    )
    return [c.to_dict() for c in engine.generate_candidates()]
