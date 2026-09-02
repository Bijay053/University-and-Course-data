"""Best-effort release identification for production logs."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache

UNKNOWN_RELEASE = "unknown (revision metadata unavailable)"
_REVISION_ENV_VARS = (
    "RELEASE_REVISION",
    "GIT_COMMIT",
    "GITHUB_SHA",
    "SOURCE_VERSION",
    "BUILD_REVISION",
)


def _safe_value(value: str) -> str:
    """Keep revision metadata single-line, bounded, and safe for log output."""
    cleaned = "".join(ch for ch in value.strip() if ch.isprintable())
    return cleaned[:128]


@lru_cache(maxsize=1)
def get_release_revision() -> str:
    """Return deployed revision metadata without ever blocking process startup."""
    try:
        for name in _REVISION_ENV_VARS:
            value = _safe_value(os.environ.get(name, ""))
            if value:
                return value

        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        return _safe_value(result.stdout) or UNKNOWN_RELEASE
    except Exception:  # noqa: BLE001 -- release logging must always fail open
        return UNKNOWN_RELEASE
