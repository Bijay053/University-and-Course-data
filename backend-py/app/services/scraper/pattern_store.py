"""Phase 3 — Autonomous Learning Layer: pattern store.

Stores and retrieves successful per-field extraction rules keyed by platform
type (e.g. ``"wordpress"``, ``"drupal"``, ``"searchstax"``, ``"sitemap_first"``).

When one university on WordPress is successfully repaired, its CSS/XPath/regex
rules are promoted here.  The next WordPress university's probe then seeds
Gemini's prompt with those proven rules, so it starts from experience rather
than from zero.

Table: ``scraper_patterns``
Columns: platform_type, field_key, rules_json (JSONB), success_count,
         avg_fill_rate, last_promoted_at, created_at
Unique constraint: (platform_type, field_key)

Public API
----------
lookup_patterns(platform_type, db)  → dict[field_key, rule_dict]
promote_patterns(platform_type, rules, fill_rates, db) → int  (rows upserted)
derive_platform_type(profile)  → str  (pure function, no DB)
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Minimum per-field fill rate to qualify a rule for promotion.
# A rule that only fills 60 % of courses isn't reliable enough to teach others.
PROMOTE_MIN_FILL_RATE: float = 0.70

# Optimistic estimated fill rate used when promoting rules from a repair pass
# before the rescrape has run (we don't yet know the true post-repair rate).
REPAIR_ESTIMATED_FILL_RATE: float = 0.75


# ── Platform type derivation ──────────────────────────────────────────────────

def derive_platform_type(profile: Any) -> str:
    """Derive a reusable platform-type key from a SiteProfile.

    Priority (highest → lowest):
    1. Explicit API provider (searchstax, algolia, solr, …) — most specific.
    2. CMS/library situation from library_strategy (wordpress, drupal, …).
    3. Recommended strategy as a coarse fallback (sitemap_first, browser, …).

    Returns an empty string when no signal is available (caller must handle).
    """
    # Explicit API provider wins — fully identifies the extraction path
    if getattr(profile, "detected_apis", None):
        provider = profile.detected_apis[0].provider
        if provider:
            return provider.lower().strip()

    # CMS/JS-framework situation from library_strategy
    ls = getattr(profile, "library_stack", None)
    if ls:
        situation = getattr(ls, "situation", None) or ""
        if situation:
            return situation.lower().strip()

    # Strategy as coarse fallback (still useful: wayback, sitemap_first, browser, …)
    strategy = getattr(profile, "recommended_strategy", None) or ""
    return strategy.lower().strip()


# ── DB operations ─────────────────────────────────────────────────────────────

async def lookup_patterns(
    platform_type: str,
    db: Any,
) -> dict[str, dict[str, Any]]:
    """Return stored extraction rules for *platform_type*.

    Returns a ``dict[field_key → rule_dict]`` ordered by avg_fill_rate DESC
    so callers see the most reliable rules first.  Returns an empty dict when:
    - *platform_type* is empty / unknown
    - the table has no rows for this platform
    - any DB error (non-fatal; logged as WARNING)
    """
    if not platform_type:
        return {}

    from sqlalchemy import text as _t
    try:
        result = await db.execute(
            _t(
                "SELECT field_key, rules_json "
                "FROM scraper_patterns "
                "WHERE platform_type = :pt "
                "ORDER BY avg_fill_rate DESC, success_count DESC"
            ),
            {"pt": platform_type},
        )
        rows = result.fetchall()
    except Exception as exc:
        log.warning("[PATTERN_STORE] lookup_patterns(%r) DB error: %s", platform_type, exc)
        return {}

    if not rows:
        log.debug("[PATTERN_STORE] no patterns for platform=%r", platform_type)
        return {}

    patterns: dict[str, dict[str, Any]] = {}
    for row in rows:
        fk, rj = row[0], row[1]
        # rules_json may already be a dict (asyncpg JSONB auto-decode)
        patterns[fk] = rj if isinstance(rj, dict) else json.loads(rj)

    log.info(
        "[PATTERN_STORE] Loaded %d learned patterns for platform=%r: %s",
        len(patterns), platform_type, sorted(patterns.keys()),
    )
    return patterns


async def promote_patterns(
    platform_type: str,
    rules: dict[str, dict[str, Any]],
    fill_rates: dict[str, float],
    db: Any,
) -> int:
    """Upsert successful extraction rules into ``scraper_patterns``.

    For each field in *rules*:
    - Skips if ``fill_rates[field]`` < :data:`PROMOTE_MIN_FILL_RATE`.
    - Upserts: on conflict updates ``rules_json``, increments ``success_count``,
      and re-computes ``avg_fill_rate`` as a running average.

    Returns the count of rows successfully upserted (0 if nothing qualified).
    Non-fatal: any per-field DB error is logged and skipped.
    """
    if not platform_type or not rules:
        return 0

    from sqlalchemy import text as _t

    promoted = 0
    for field_key, rule in rules.items():
        rate = fill_rates.get(field_key, 0.0)
        if rate < PROMOTE_MIN_FILL_RATE:
            log.debug(
                "[PATTERN_STORE] skip %s/%s — fill_rate %.2f < %.2f",
                platform_type, field_key, rate, PROMOTE_MIN_FILL_RATE,
            )
            continue
        try:
            await db.execute(
                _t("""
                    INSERT INTO scraper_patterns
                        (platform_type, field_key, rules_json,
                         success_count, avg_fill_rate, last_promoted_at)
                    VALUES
                        (:pt, :fk, :rj::jsonb, 1, :rate, now())
                    ON CONFLICT (platform_type, field_key) DO UPDATE SET
                        rules_json       = EXCLUDED.rules_json,
                        success_count    = scraper_patterns.success_count + 1,
                        avg_fill_rate    = (
                            scraper_patterns.avg_fill_rate
                            * scraper_patterns.success_count
                            + EXCLUDED.avg_fill_rate
                        ) / (scraper_patterns.success_count + 1),
                        last_promoted_at = now()
                """),
                {
                    "pt": platform_type,
                    "fk": field_key,
                    "rj": json.dumps(rule),
                    "rate": rate,
                },
            )
            promoted += 1
        except Exception as exc:
            log.warning(
                "[PATTERN_STORE] upsert failed for %s/%s: %s",
                platform_type, field_key, exc,
            )

    if promoted:
        try:
            await db.commit()
        except Exception as exc:
            log.warning("[PATTERN_STORE] commit failed after promote: %s", exc)
            return 0

    log.info(
        "[PATTERN_STORE] Promoted %d/%d rules for platform=%r (threshold=%.0f%%)",
        promoted, len(rules), platform_type, PROMOTE_MIN_FILL_RATE * 100,
    )
    return promoted
