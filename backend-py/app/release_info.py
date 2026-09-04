"""Best-effort release identification for production logs."""
from __future__ import annotations

import os
import re
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
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _safe_value(value: str) -> str:
    """Keep revision metadata single-line, bounded, and safe for log output."""
    cleaned = "".join(ch for ch in value.strip() if ch.isprintable())
    return cleaned[:128]


@lru_cache(maxsize=1)
def get_release_revision() -> str:
    """Return deployed revision metadata without ever blocking process startup."""
    try:
        env_revision = ""
        for name in _REVISION_ENV_VARS:
            value = _safe_value(os.environ.get(name, ""))
            if value:
                env_revision = value
                break

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        git_revision = _safe_value(result.stdout)
        if (
            env_revision
            and _GIT_SHA_RE.fullmatch(env_revision)
            and _GIT_SHA_RE.fullmatch(git_revision)
            and not git_revision.lower().startswith(env_revision.lower())
        ):
            return git_revision[:12]
        return env_revision or git_revision[:12] or UNKNOWN_RELEASE
    except Exception:  # noqa: BLE001 -- release logging must always fail open
        return env_revision or UNKNOWN_RELEASE
