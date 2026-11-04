"""Exercise the real smoke import in fresh, network-free Python processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKER = "CREDENTIAL_CONTENT_MUST_NOT_APPEAR"
GOOD = (
    'DATABASE_URL="postgresql://test:dummy@managed.invalid/test"\n'
    'DATABASE_REQUIRE_TLS=true\n'
    'DATABASE_SECRET_VERSION="test-version"\n'
    'SSL_CERT_FILE="/test-only/ca.pem"\n'
)

# Stop at the first database import, before any connection/settings engine or
# release command can run. Invalid configuration must never reach this guard.
CHILD = r"""
import importlib.abc
import json
import os
from pathlib import Path
import runpy
import sys
import traceback
from app import managed_database_environment as managed

managed.MANAGED_DATABASE_ENV_FILE = Path(sys.argv[1])
if sys.argv[2] == "unreadable":
    class Unreadable:
        def read_text(self, **kwargs):
            raise PermissionError("CREDENTIAL_CONTENT_MUST_NOT_APPEAR")
    managed.MANAGED_DATABASE_ENV_FILE = Unreadable()

class StopBeforeDatabase(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "app.database":
            raise SystemExit("DATABASE_IMPORT_GUARD")

sys.meta_path.insert(0, StopBeforeDatabase())
sys.path.insert(0, str(Path("deploy").resolve()))  # Match direct CLI execution.
before = {key: os.environ.get(key) for key in
          ("DATABASE_URL", "DATABASE_REQUIRE_TLS", "DATABASE_SECRET_VERSION", "SSL_CERT_FILE")}
try:
    runpy.run_path("deploy/safe_restart_smoke.py", run_name="isolated_smoke_import")
except BaseException as error:
    reached = isinstance(error, SystemExit) and str(error) == "DATABASE_IMPORT_GUARD"
    print(json.dumps({
        "databaseImportReached": reached,
        "environmentUnchanged": all(os.environ.get(key) == value for key, value in before.items()),
        "managedLoaded": os.environ.get("DATABASE_URL") == "postgresql://test:dummy@managed.invalid/test"
                         and os.environ.get("DATABASE_REQUIRE_TLS") == "true"
                         and os.environ.get("DATABASE_SECRET_VERSION") == "test-version"
                         and os.environ.get("SSL_CERT_FILE") == "/test-only/ca.pem",
        "error": "" if reached else traceback.format_exc(),
    }))
    sys.exit(0)
raise AssertionError("Smoke import unexpectedly completed beyond the database fence")
"""


def probe(path, mode="normal"):
    # Do not pass real workspace credentials into the child process.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(PYTHONPATH=str(ROOT), DATABASE_URL="postgresql://stale.invalid/test",
               DATABASE_REQUIRE_TLS="false", DATABASE_SECRET_VERSION="before")
    result = subprocess.run([sys.executable, "-c", CHILD, str(path), mode], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert MARKER not in result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("content", [
    "",
    f'DATABASE_URL="{MARKER}"\n',
    f'DATABASE_URL="{MARKER}"\nDATABASE_REQUIRE_TLS=false\n',
    f'DATABASE_URL="{MARKER}"\nDATABASE_REQUIRE_TLS=0\n',
    f'DATABASE_URL="{MARKER}"\nDATABASE_REQUIRE_TLS=optional\n',
    f'DATABASE_URL="{MARKER}\nDATABASE_REQUIRE_TLS=true\n',
    GOOD + f"malformed trailing {MARKER}\n",
    GOOD + f'DATABASE_URL="{MARKER}"\n',
    GOOD + "DATABASE_REQUIRE_TLS=true\n",
    GOOD + f'DATABASE_SECRET_VERSION="{MARKER}"\n',
    GOOD + f'SSL_CERT_FILE="{MARKER}"\n',
    f'DATABASE_URL="{MARKER}" ignored-token\nDATABASE_REQUIRE_TLS=true\n',
    'DATABASE_URL=""\nDATABASE_REQUIRE_TLS=true\n',
])
def test_invalid_config_is_atomic_redacted_and_stops_before_database(tmp_path, content):
    path = tmp_path / "database.env"
    path.write_text(content)
    result = probe(path)
    assert not result["databaseImportReached"]
    assert result["environmentUnchanged"]
    assert "Managed database configuration is invalid" in result["error"]


def test_missing_file_on_managed_host_fails_closed(tmp_path):
    result = probe(tmp_path / "database.env")
    assert not result["databaseImportReached"] and result["environmentUnchanged"]
    assert "Managed database configuration is missing" in result["error"]


@pytest.mark.parametrize("mode", ["bad-encoding", "unreadable"])
def test_read_errors_are_redacted_and_stop_import(tmp_path, mode):
    path = tmp_path / "database.env"
    path.write_bytes(b"\xff" + MARKER.encode())
    result = probe(path, mode)
    assert not result["databaseImportReached"] and result["environmentUnchanged"]
    assert "Managed database configuration is unreadable" in result["error"]


def test_valid_managed_settings_override_stale_environment(tmp_path):
    path = tmp_path / "database.env"
    path.write_text("# operator settings\n" + GOOD + "# trailing comment\n")
    result = probe(path)
    assert result["databaseImportReached"] and result["managedLoaded"], result["error"]
    assert not result["environmentUnchanged"] and not result["error"]


def test_unmanaged_development_environment_is_unchanged(tmp_path):
    result = probe(tmp_path / "unmanaged" / "database.env")
    assert result["databaseImportReached"] and result["environmentUnchanged"], result["error"]
