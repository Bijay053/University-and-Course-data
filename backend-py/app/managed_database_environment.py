"""Load the host-managed database configuration without logging its contents."""
from __future__ import annotations

import os
from pathlib import Path
import shlex

MANAGED_DATABASE_ENV_FILE = Path("/etc/university-portal/database.env")
_KEYS = {"DATABASE_URL", "DATABASE_REQUIRE_TLS", "DATABASE_SECRET_VERSION", "SSL_CERT_FILE"}


def load_managed_database_environment() -> None:
    """Managed configuration wins; an absent unmanaged host keeps dev defaults.

    A provisioned managed directory with missing/broken configuration is an
    error, not permission to use a possibly stale plaintext database URL.
    Parse completely before changing the environment. Never echo input/errors
    from the parser, since they can contain credentials.
    """
    path = MANAGED_DATABASE_ENV_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not path.parent.exists():
            return
        raise RuntimeError("Managed database configuration is missing") from None
    except (OSError, UnicodeError):
        raise RuntimeError("Managed database configuration is unreadable") from None

    values: dict[str, str] = {}
    try:
        for line in text.splitlines():
            tokens = shlex.split(line, comments=True, posix=True)
            if not tokens:
                continue
            if len(tokens) != 1 or "=" not in tokens[0]:
                raise ValueError
            key, value = tokens[0].split("=", 1)
            if key in _KEYS:
                if key in values:
                    raise ValueError
                values[key] = value
        if not values.get("DATABASE_URL"):
            raise ValueError
        if values.get("DATABASE_REQUIRE_TLS", "").lower() != "true":
            raise ValueError
    except ValueError:
        raise RuntimeError("Managed database configuration is invalid; verified TLS is required") from None
    os.environ.update(values)
