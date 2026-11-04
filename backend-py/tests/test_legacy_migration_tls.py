"""Offline connection-policy tests: never execute DDL against any real database."""
import ast
import importlib
import importlib.util
from pathlib import Path
import ssl
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from app import managed_database_environment as managed
from app.database import postgres_tls_connect_args  # import before factory spies

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("apply_migration_*.py"))
TEST_URL = "postgres://dummy%40user:dummy%25pass@managed.invalid/test?sslmode=require"


@pytest.fixture
def secure_module(monkeypatch, tmp_path):
    import app
    import sqlalchemy.ext.asyncio

    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("DATABASE_REQUIRE_TLS", "false")
    # Record the original state even when this key was absent. The loader
    # writes directly to os.environ, and delenv on an absent key has no undo.
    monkeypatch.setenv("SSL_CERT_FILE", "fixture-placeholder")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(managed, "MANAGED_DATABASE_ENV_FILE", tmp_path / "absent" / "database.env")
    monkeypatch.delitem(sys.modules, "app.legacy_migration_database", raising=False)
    monkeypatch.delattr(app, "legacy_migration_database", raising=False)
    calls = []

    def factory(url, **options):
        calls.append((url, options))
        return SimpleNamespace(url=make_url(url))

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", factory)
    module = importlib.import_module("app.legacy_migration_database")
    calls.clear()
    return module, calls


def managed_file(monkeypatch, tmp_path, content):
    path = tmp_path / "managed" / "database.env"
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    monkeypatch.setattr(managed, "MANAGED_DATABASE_ENV_FILE", path)
    return path


def test_fresh_managed_environment_wins_over_cached_settings(secure_module, monkeypatch, tmp_path):
    module, calls = secure_module
    managed_file(monkeypatch, tmp_path, f'DATABASE_URL="{TEST_URL}"\nDATABASE_REQUIRE_TLS=true\n')
    monkeypatch.setenv("DATABASE_URL", "postgresql://stale.invalid/wrong")
    module.create_migration_engine(echo=False)
    url, options = calls[-1]
    parsed = make_url(url)
    assert parsed.host == "managed.invalid"
    assert parsed.username == "dummy@user" and parsed.password == "dummy%pass"
    assert not parsed.query
    context = options["connect_args"]["ssl"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("bad", [
    "DATABASE_URL=secret-marker\nDATABASE_REQUIRE_TLS=false",
    "DATABASE_URL=secret-marker",
    "DATABASE_URL='unterminated-secret-marker",
])
def test_bad_managed_file_stops_before_engine_creation(secure_module, monkeypatch, tmp_path, bad):
    module, calls = secure_module
    managed_file(monkeypatch, tmp_path, bad)
    with pytest.raises(RuntimeError) as error:
        module.create_migration_engine()
    assert "secret-marker" not in str(error.value)
    assert not calls


def test_missing_managed_file_stops_before_engine_creation(secure_module, monkeypatch, tmp_path):
    module, calls = secure_module
    path = managed_file(monkeypatch, tmp_path, "")
    path.unlink()
    with pytest.raises(RuntimeError):
        module.create_migration_engine()
    assert not calls


def test_missing_ca_has_no_insecure_retry(secure_module, monkeypatch, tmp_path):
    module, calls = secure_module
    managed_file(monkeypatch, tmp_path, f'DATABASE_URL="{TEST_URL}"\nDATABASE_REQUIRE_TLS=true\n'
                 f'SSL_CERT_FILE="{tmp_path / "missing.pem"}"\n')
    with pytest.raises(FileNotFoundError):
        module.create_migration_engine()
    assert not calls


def test_operator_ca_is_explicitly_loaded(secure_module, monkeypatch, tmp_path):
    import certifi
    module, calls = secure_module
    managed_file(monkeypatch, tmp_path, f'DATABASE_URL="{TEST_URL}"\nDATABASE_REQUIRE_TLS=true\n'
                 f'SSL_CERT_FILE="{certifi.where()}"\n')
    module.create_migration_engine()
    assert calls[-1][1]["connect_args"]["ssl"].cert_store_stats()["x509_ca"] > 0


def test_unmanaged_development_and_no_connection_overrides(secure_module):
    module, calls = secure_module
    module.create_migration_engine()
    assert calls[-1][1]["connect_args"] == {}
    for options in [{"url": TEST_URL}, {"connect_args": {"ssl": False}}]:
        with pytest.raises(ValueError):
            module.create_migration_engine(**options)
    assert len(calls) == 1


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_every_entrypoint_uses_the_secure_adapter(path):
    tree = ast.parse(path.read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert any(node.module == "app.legacy_migration_database" for node in imports)
    assert not any(node.module in {"app.database", "app.config", "asyncpg"} for node in imports)
    assert not any(node.module == "sqlalchemy.ext.asyncio"
                   and any(alias.name == "create_async_engine" for alias in node.names) for node in imports)
    assert not any(isinstance(node, ast.Name) and node.id in {"DATABASE_URL", "DB_URL", "_raw_url"}
                   for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Import) and any(a.name in {"asyncpg", "subprocess"} for a in node.names)
                   for node in ast.walk(tree))


def load_script(filename):
    spec = importlib.util.spec_from_file_location("legacy_test_script", ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["apply_migration_387_local.py", "apply_migration_390_local.py"])
async def test_local_only_repairs_still_reject_remote_hosts(secure_module, monkeypatch, filename):
    module = load_script(filename)
    monkeypatch.setattr(module, "engine", SimpleNamespace(url=make_url(TEST_URL)))
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("198.51.100.42", 5432))])
    with pytest.raises(SystemExit, match="not verified local"):
        await module.main()


@pytest.mark.asyncio
@pytest.mark.parametrize("number,count", [("032", 4), ("033", 6)])
@pytest.mark.parametrize("fail", [False, True])
async def test_simple_ddl_keeps_one_transaction_and_disposes_on_failure(secure_module, monkeypatch, number, count, fail):
    module = load_script(f"apply_migration_{number}.py")
    statements = []
    state = {"committed": False, "disposed": False}

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, kind, *args):
            if number == "033":
                state["committed"] = kind is None

        async def execute(self, statement):
            statements.append(str(statement))
            if fail and len(statements) == 2:
                raise RuntimeError("injected statement failure")

        async def commit(self):
            state["committed"] = True

    class Engine:
        def begin(self):
            return Connection()

        async def dispose(self):
            state["disposed"] = True

    monkeypatch.setattr(module, "create_migration_engine", lambda **kw: Engine())
    if number == "032":
        monkeypatch.setattr(module, "async_sessionmaker", lambda *a, **kw: Connection)
    run = module._apply if number == "032" else module.main
    if fail:
        with pytest.raises(RuntimeError, match="injected"):
            await run()
        assert len(statements) == 2 and not state["committed"]
    else:
        await run()
        assert len(statements) == count and state["committed"]
        assert statements == [item.strip() for item in module.SQL.split(";") if item.strip()]
    assert state["disposed"]
