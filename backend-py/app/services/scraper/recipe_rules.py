"""Apply operator-configured recipe rules to an extracted course payload.

All functions are pure transforms — they modify the payload dict in-place
and return it.  Called by the orchestrator BEFORE stage_course(), so
rule-transformed values are stored with full provenance.

Rule sections:
  - course name cleanup  (remove_after, remove_patterns, remove_year_suffix)
  - IELTS component mapping  (overall → each-band lookup)
  - location cleanup  (replace, reject, allowed-values filter)
  - study mode derivation from location  (optional)
  - fee term override and explicit fee conversion
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
    _apply_currency_override(payload, recipe)
    _apply_fee_term_override(payload, recipe)
    _apply_ielts_component_mapping(payload, recipe)
    _apply_location_rules(payload, recipe)
    _apply_study_mode_from_location(payload, recipe)
    _apply_degree_mapping(payload, recipe)
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

def _apply_currency_override(payload: dict, recipe: dict) -> None:
    """Apply an explicitly configured fee currency at the final boundary."""
    currency = str(recipe.get("currency_override") or "").strip().upper()
    if not currency or not payload.get("international_fee"):
        return
    previous = payload.get("currency") or payload.get("fee_currency")
    payload["currency"] = currency
    payload["fee_currency"] = currency
    if previous != currency:
        log.info("[RECIPE] currency forced %r → %r", previous, currency)


def _apply_fee_term_override(payload: dict, recipe: dict) -> None:
    """Apply fee_term and fee_calculation_mode rules.

    fee_prevent_full_course_rollup:
      Legacy compatibility field. It must never relabel a Full Course total as
      Annual while keeping the amount unchanged.

    fee_term override:
      Forces fee_term to the configured value regardless of what was extracted.
      NOTE: do NOT combine with fee_calculation_mode='full_course_to_annual' —
      the forced term replaces 'Full Course' before the conversion can run.

    max_annual_fee:
      If an extracted Annual fee exceeds this cap, it is discarded as a likely
      total-course value misidentified as annual (e.g. Gemini returning the
      3-year total instead of the per-year fee).
    """
    # Explicit fee_term override
    forced_term = recipe.get("fee_term")
    if forced_term and payload.get("fee_term"):
        if payload["fee_term"] != forced_term:
            log.info("[RECIPE] fee_term forced %r → %r", payload["fee_term"], forced_term)
        payload["fee_term"] = forced_term

    # fee_calculation_mode: use_source_value_only (default) — no conversion
    mode = recipe.get("fee_calculation_mode", "use_source_value_only")
    if mode == "use_source_value_only":
        pass  # No conversion — keep extracted amount exactly
    elif mode == "full_course_to_annual":
        _convert_full_course_to_annual(payload)
    elif mode == "per_unit_to_annual":
        _convert_per_unit_to_annual(payload)

    # max_annual_fee sanity cap: discard Annual fees that look like total course
    # costs (e.g. Gemini returning 3-year total when page text is ambiguous).
    max_fee = recipe.get("max_annual_fee")
    if max_fee and payload.get("fee_term") == "Annual":
        fee = payload.get("international_fee")
        if isinstance(fee, (int, float)) and fee > max_fee:
            log.warning(
                "[RECIPE] max_annual_fee: %.0f > %.0f — discarding likely total-course "
                "fee misidentified as Annual",
                fee,
                max_fee,
            )
            payload["international_fee"] = None
            payload["fee_term"] = None


def _convert_full_course_to_annual(payload: dict) -> None:
    """Divide a Full Course total fee by duration to get the annual equivalent.

    Only fires when fee_term == 'Full Course'.  For courses shorter than 1 year
    the full-course total IS effectively the per-period fee, so we just relabel
    it Annual without dividing (dividing by e.g. 0.5 would double the number).
    """
    if payload.get("fee_term") != "Full Course":
        return
    fee = payload.get("international_fee")
    dur = payload.get("duration")
    dur_term = (payload.get("duration_term") or "").lower()
    if not fee or not dur:
        return
    years: float | None = None
    if "year" in dur_term:
        years = float(dur)
    elif "month" in dur_term:
        years = float(dur) / 12.0
    elif "week" in dur_term:
        years = float(dur) / 52.0
    if years is None or years <= 0:
        return
    # Sub-annual course: full-course fee == annual/period fee — don't inflate
    if years < 1.0:
        payload["fee_term"] = "Annual"
        log.info(
            "[RECIPE] full_course_to_annual: duration %.2f yr < 1 — keeping %s as Annual",
            years,
            fee,
        )
        return
    annual = round(fee / years)
    log.info("[RECIPE] full_course_to_annual: %s / %.2f yr = %s", fee, years, annual)
    payload["international_fee"] = annual
    payload["fee_term"] = "Annual"


def _convert_per_unit_to_annual(payload: dict) -> None:
    """Multiply per-unit fee by 8 (default credit-point load) for an annual estimate."""
    fee = payload.get("international_fee")
    if fee and fee < 3000:  # Likely a per-unit amount
        annual = round(fee * 8)
        log.info("[RECIPE] per_unit_to_annual: %s × 8 units = %s", fee, annual)
        payload["international_fee"] = annual
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


# ── Degree level mapping ───────────────────────────────────────────────────────

def _apply_degree_mapping(payload: dict, recipe: dict) -> None:
    """Normalise degree_level using operator-configured keyword mapping.

    Recipe key: ``degree_mapping`` — dict of canonical_level → [keyword, ...]

    Example::

        degree_mapping:
          Bachelor:
            - Bachelor
            - BSc
            - BA
            - BBus
          Master:
            - Master
            - MSc
            - MBA

    The first canonical whose keyword list contains a case-insensitive
    substring match against the extracted degree_level is applied.
    Subsequent canonicals are not checked (first-match wins).
    No-op when degree_level is already blank.
    """
    mapping = recipe.get("degree_mapping") or {}
    if not mapping:
        return
    current = (payload.get("degree_level") or "").strip()
    if not current:
        return
    for canonical, keywords in mapping.items():
        if any(kw.lower() in current.lower() for kw in (keywords or [])):
            if current != canonical:
                log.info("[RECIPE] degree_mapping: %r → %r", current, canonical)
                payload["degree_level"] = canonical
            return  # first match wins
