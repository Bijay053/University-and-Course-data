"""Apply operator-configured recipe rules to an extracted course payload.

All functions are pure transforms — they modify the payload dict in-place
and return it.  Called by the orchestrator BEFORE stage_course(), so
rule-transformed values are stored with full provenance.

Rule sections:
  - course name cleanup  (remove_after, remove_patterns, remove_year_suffix)
  - IELTS component mapping  (overall → each-band lookup)
  - location cleanup  (replace, reject, allowed-values filter)
  - study mode derivation from location  (optional)
  - fee term override  (prevent Full Course rollup)
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ── Public entry point ────────────────────────────────────────────────────────

def apply_recipe_rules(payload: dict[str, Any], recipe: dict) -> dict[str, Any]:
    """Apply all configured recipe rules to the payload.

    Called with the raw extraction payload dict before it is passed to
    stage_course().  Returns the same dict (modified in-place).
    """
    if not recipe:
        return payload

    _apply_course_name_cleanup(payload, recipe)
    _apply_fee_term_override(payload, recipe)
    _apply_ielts_component_mapping(payload, recipe)
    _apply_location_rules(payload, recipe)
    _apply_study_mode_from_location(payload, recipe)
    return payload


# ── Course name cleanup ───────────────────────────────────────────────────────

def _apply_course_name_cleanup(payload: dict, recipe: dict) -> None:
    """Strip suffixes, patterns, and year tags from the course name."""
    name = payload.get("course_name") or payload.get("name")
    if not name:
        return

    original = name

    # 1. remove_after: chop everything from the first occurrence of any marker
    for marker in (recipe.get("course_name_remove_after") or []):
        idx = name.find(marker)
        if idx > 0:
            name = name[:idx].strip()
            log.debug("[RECIPE] course_name remove_after %r → %r", marker, name)

    # 2. remove_patterns: regex removal (case-insensitive)
    for pattern in (recipe.get("course_name_remove_patterns") or []):
        try:
            name = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()
        except re.error as e:
            log.warning("[RECIPE] course_name_remove_patterns bad regex %r: %s", pattern, e)

    # 3. remove_year_suffix: strip trailing 4-digit year
    if recipe.get("course_name_remove_year_suffix"):
        name = re.sub(r"\s+20\d{2}\s*$", "", name).strip()

    name = re.sub(r"\s+", " ", name).strip()

    if name != original:
        log.info("[RECIPE] course_name cleaned: %r → %r", original, name)
        if "course_name" in payload:
            payload["course_name"] = name
        if "name" in payload:
            payload["name"] = name


# ── Fee term override ─────────────────────────────────────────────────────────

def _apply_fee_term_override(payload: dict, recipe: dict) -> None:
    """Apply fee_term and fee_calculation_mode rules.

    fee_prevent_full_course_rollup=True (default when fee_source_urls set):
      If fee_term on the payload is 'Full Course' and duration is known,
      revert to the raw amount but mark as 'Annual' — the operator wants
      the source page value, not a computed total.

    fee_term override:
      Forces fee_term to the configured value regardless of what was extracted.
    """
    # Explicit fee_term override
    forced_term = recipe.get("fee_term")
    if forced_term and payload.get("fee_term"):
        if payload["fee_term"] != forced_term:
            log.info("[RECIPE] fee_term forced %r → %r", payload["fee_term"], forced_term)
        payload["fee_term"] = forced_term

    # Prevent Full Course rollup
    if recipe.get("fee_prevent_full_course_rollup", True):
        if payload.get("fee_term") == "Full Course":
            # Revert: mark as Annual so the UI shows the right value
            # The amount is already what the fee page shows (Annual or Total);
            # since prevent_rollup=True, trust it as Annual.
            payload["fee_term"] = "Annual"
            log.info(
                "[RECIPE] fee_prevent_full_course_rollup: fee_term Full Course → Annual "
                "(amount %s kept as-is)",
                payload.get("annual_tuition_fee"),
            )

    # fee_calculation_mode: use_source_value_only (default) — no conversion
    mode = recipe.get("fee_calculation_mode", "use_source_value_only")
    if mode == "use_source_value_only":
        pass  # No conversion — keep extracted amount exactly
    elif mode == "full_course_to_annual":
        _convert_full_course_to_annual(payload)
    elif mode == "per_unit_to_annual":
        _convert_per_unit_to_annual(payload)


def _convert_full_course_to_annual(payload: dict) -> None:
    """Divide Full Course fee by duration to get annual equivalent."""
    fee = payload.get("annual_tuition_fee")
    dur = payload.get("duration")
    dur_term = (payload.get("duration_term") or "").lower()
    if not fee or not dur:
        return
    years = dur if "year" in dur_term else (dur / 12 if "month" in dur_term else None)
    if years and years > 0:
        annual = round(fee / years)
        log.info("[RECIPE] full_course_to_annual: %s / %sy = %s", fee, years, annual)
        payload["annual_tuition_fee"] = annual
        payload["fee_term"] = "Annual"


def _convert_per_unit_to_annual(payload: dict) -> None:
    """Multiply per-unit fee by 8 (default credit-point load) for an annual estimate."""
    fee = payload.get("annual_tuition_fee")
    if fee and fee < 3000:  # Likely a per-unit amount
        annual = round(fee * 8)
        log.info("[RECIPE] per_unit_to_annual: %s × 8 units = %s", fee, annual)
        payload["annual_tuition_fee"] = annual
        payload["fee_term"] = "Annual"


# ── IELTS component mapping ───────────────────────────────────────────────────

def _apply_ielts_component_mapping(payload: dict, recipe: dict) -> None:
    """Fill IELTS component scores from overall band using operator mapping table.

    Example recipe config:
        ielts_component_mapping: {"6.0": 5.5, "6.5": 6.0, "7.0": 6.5}

    If ielts_overall is 7.0 and no components are set, this writes:
        ielts_reading = ielts_writing = ielts_listening = ielts_speaking = 6.5
    """
    mapping = recipe.get("ielts_component_mapping") or {}
    if not mapping:
        return

    overall = payload.get("ielts_overall")
    if overall is None:
        return

    # Skip if any component already populated
    components = ["ielts_reading", "ielts_writing", "ielts_listening", "ielts_speaking"]
    if any(payload.get(k) is not None for k in components):
        return

    # Look up by float or string key
    band = mapping.get(overall) or mapping.get(str(overall)) or mapping.get(str(float(overall)))
    if band is None:
        # Try rounding to nearest 0.5
        rounded = round(overall * 2) / 2
        band = mapping.get(rounded) or mapping.get(str(rounded))

    if band is not None:
        band = float(band)
        for k in components:
            payload[k] = band
        log.info(
            "[RECIPE] ielts_component_mapping: overall=%.1f → each band=%.1f",
            overall,
            band,
        )


# ── Location cleanup ──────────────────────────────────────────────────────────

def _apply_location_rules(payload: dict, recipe: dict) -> None:
    """Apply location replace → reject → allowed-values filter rules.

    Order matters:
    1. Apply replace map (e.g. 'SCU Online' → 'Online')
    2. Reject if any reject_values keyword found
    3. Filter to allowed_values (keep only matching entries)
    """
    loc = payload.get("location") or payload.get("course_location")
    if not loc:
        return

    original = loc

    # 1. Replace map
    for old_val, new_val in (recipe.get("location_replace") or {}).items():
        loc = loc.replace(old_val, str(new_val))
    loc = loc.strip()

    # 2. Reject keywords
    reject = [v.lower() for v in (recipe.get("location_reject_values") or [])]
    if reject and any(r in loc.lower() for r in reject):
        log.info("[RECIPE] location_reject_values cleared location: %r", loc)
        _clear_location(payload)
        return

    # 3. Allowed values filter — keep only matching items from the allowlist
    allowed = recipe.get("location_allowed_values") or []
    if allowed:
        matched = [a for a in allowed if a.lower() in loc.lower()]
        if matched:
            loc = ", ".join(matched)
        else:
            log.info("[RECIPE] location not in allowed_values, clearing: %r", loc)
            _clear_location(payload)
            return

    if loc != original:
        log.info("[RECIPE] location cleaned: %r → %r", original, loc)

    if "location" in payload:
        payload["location"] = loc
    if "course_location" in payload:
        payload["course_location"] = loc


def _clear_location(payload: dict) -> None:
    if "location" in payload:
        payload["location"] = None
    if "course_location" in payload:
        payload["course_location"] = None


# ── Study mode derivation ─────────────────────────────────────────────────────

def _apply_study_mode_from_location(payload: dict, recipe: dict) -> None:
    """Derive study_mode from the cleaned location when study_mode is blank.

    Online keywords (default: online, distance, virtual) are checked against
    the location string.  If the location also contains campus names, the
    result is Blended; if only online keywords match, the result is Online;
    otherwise On Campus.
    """
    if not recipe.get("study_mode_from_location"):
        return

    # Only fill in if blank — don't overwrite a successfully extracted mode
    if payload.get("study_mode"):
        return

    loc = (payload.get("location") or payload.get("course_location") or "").lower()
    if not loc:
        return

    kws = [k.lower() for k in (recipe.get("study_mode_online_keywords") or ["online", "distance", "virtual"])]
    has_online = any(k in loc for k in kws)
    has_non_online = any(k not in loc for k in kws)  # crude: location has content beyond online kws

    if has_online and has_non_online:
        payload["study_mode"] = "Blended"
    elif has_online:
        payload["study_mode"] = "Online"
    else:
        payload["study_mode"] = "On Campus"

    log.info("[RECIPE] study_mode_from_location: loc=%r → mode=%r", loc, payload["study_mode"])
