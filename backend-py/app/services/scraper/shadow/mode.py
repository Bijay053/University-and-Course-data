"""Per-uni shadow mode and cutover flag parsing.

Enable shadow mode via SHADOW_MODE_UNI_IDS:
    SHADOW_MODE_UNI_IDS=41           # ACAP only
    SHADOW_MODE_UNI_IDS=41,20,87     # multiple unis
    SHADOW_MODE_UNI_IDS=*            # all unis (danger: use with care)

Cutover (flip which path is authoritative) via SHADOW_CUTOVER_UNI_IDS:
    SHADOW_CUTOVER_UNI_IDS=41        # ACAP has completed 5-run streak

Shadow mode and cutover are independent: a uni can be in shadow mode (still
verifying) or in cutover (new path is authoritative), but not both.
"""

import os


def _parse_uni_ids(env_var: str) -> frozenset[int] | None:
    """Parse comma-separated uni IDs.

    Returns:
        None      — env var not set → feature disabled
        frozenset() — wildcard '*' → all unis
        frozenset({41, 20}) — explicit list
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    if raw == "*":
        return frozenset()
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    try:
        return frozenset(int(p) for p in parts)
    except ValueError:
        return None


def is_shadow_enabled(uni_id: int) -> bool:
    """Return True if shadow mode (dual extraction, diff only) is active for uni_id."""
    ids = _parse_uni_ids("SHADOW_MODE_UNI_IDS")
    if ids is None:
        return False
    if len(ids) == 0:
        return True
    return uni_id in ids


def is_cutover(uni_id: int) -> bool:
    """Return True if this uni has passed 5-run streak and new path is now authoritative."""
    ids = _parse_uni_ids("SHADOW_CUTOVER_UNI_IDS")
    if ids is None:
        return False
    if len(ids) == 0:
        return True
    return uni_id in ids
