"""No production connections: launcher tests use fake engines or in-memory SQLite."""
from contextlib import nullcontext
from pathlib import Path
import runpy
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic import context
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import managed_database_environment as managed
from app.config import Settings
from app.database import postgres_tls_connect_args

ROOT = Path(__file__).resolve().parents[1]
URL = "postgres://test%40user:dummy%25%40%3Apass@localhost/test?sslmode=require&channel_binding=require"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(managed, "MANAGED_DATABASE_ENV_FILE", tmp_path / "unmanaged" / "database.env")
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("DATABASE_REQUIRE_TLS", "false")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


def test_required_tls_and_explicit_development():
    assert postgres_tls_connect_args(SimpleNamespace(database_require_tls=False)) == {}
    tls = postgres_tls_connect_args(SimpleNamespace(database_require_tls=True))["ssl"]
    assert tls.verify_mode == ssl.CERT_REQUIRED
    assert tls.check_hostname is True


def test_custom_ca_bundle_and_missing_bundle(monkeypatch, tmp_path):
    import certifi

    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    tls = postgres_tls_connect_args(SimpleNamespace(database_require_tls=True))["ssl"]
    assert tls.cert_store_stats()["x509_ca"] > 0
    assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(FileNotFoundError):
        postgres_tls_connect_args(SimpleNamespace(database_require_tls=True))


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_normalization_preserves_encoded_credentials(scheme):
    settings = Settings(_env_file=None, database_url=URL.replace("postgres:", f"{scheme}:"))
    url = make_url(settings.database_url)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "test@user"
    assert url.password == "dummy%@:pass"
    assert not url.query


def write_managed(monkeypatch, tmp_path, content):
    path = tmp_path / "managed" / "database.env"
    path.parent.mkdir()
    path.write_text(content)
    monkeypatch.setattr(managed, "MANAGED_DATABASE_ENV_FILE", path)
    return path


@pytest.mark.parametrize("content", [
    'DATABASE_URL="secret-marker"\nDATABASE_REQUIRE_TLS=false',
    'DATABASE_URL="secret-marker"\n',
    'DATABASE_URL="secret-marker',
    'DATABASE_URL="secret-marker"\nDATABASE_REQUIRE_TLS=true\nDATABASE_REQUIRE_TLS=false',
])
def test_invalid_managed_config_fails_without_secrets(monkeypatch, tmp_path, content):
    write_managed(monkeypatch, tmp_path, content)
    with pytest.raises(RuntimeError) as error:
        managed.load_managed_database_environment()
    assert "secret-marker" not in str(error.value)
    assert Settings(_env_file=None).database_require_tls is False


def test_missing_managed_file_fails_closed(monkeypatch, tmp_path):
    path = write_managed(monkeypatch, tmp_path, "")
    path.unlink()
    with pytest.raises(RuntimeError, match="missing"):
        managed.load_managed_database_environment()


def launcher_context(monkeypatch):
    config = Config()
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(context, "configure", MagicMock())
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", MagicMock())
    return config


@pytest.mark.parametrize("fail", [False, True])
def test_launcher_loads_managed_tls_and_disposes(monkeypatch, tmp_path, fail):
    import sqlalchemy.ext.asyncio

    write_managed(monkeypatch, tmp_path, f'DATABASE_URL="{URL}"\nDATABASE_REQUIRE_TLS=true\n')
    config = launcher_context(monkeypatch)
    connection = SimpleNamespace(run_sync=AsyncMock(side_effect=RuntimeError("migration failed") if fail else None))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=connection)
    manager.__aexit__ = AsyncMock(return_value=False)
    engine = SimpleNamespace(connect=lambda: manager, dispose=AsyncMock())
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_engine_from_config", factory)
    if fail:
        with pytest.raises(RuntimeError, match="migration failed"):
            runpy.run_path(str(ROOT / "alembic/env.py"))
    else:
        runpy.run_path(str(ROOT / "alembic/env.py"))
    args, kwargs = factory.call_args
    tls = kwargs["connect_args"]["ssl"]
    assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
    assert make_url(args[0]["sqlalchemy.url"]).password == "dummy%@:pass"
    assert config.get_main_option("sqlalchemy.url") == args[0]["sqlalchemy.url"]
    engine.dispose.assert_awaited_once()


def test_offline_launcher_never_opens_engine(monkeypatch):
    import sqlalchemy.ext.asyncio

    launcher_context(monkeypatch)
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    factory = MagicMock(side_effect=AssertionError("offline engine"))
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_engine_from_config", factory)
    runpy.run_path(str(ROOT / "alembic/env.py"))
    assert make_url(context.configure.call_args.kwargs["url"]).password == "dummy%@:pass"
    factory.assert_not_called()


@pytest.mark.parametrize("fail", [False, True])
def test_caller_transaction_is_not_committed_or_closed(monkeypatch, fail):
    import sqlalchemy.ext.asyncio

    factory = MagicMock(side_effect=AssertionError("must not create another engine"))
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_engine_from_config", factory)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE test_rows (value INTEGER)"))
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    def migrate(rev, migration_context):
        migration_context.connection.execute(text("INSERT INTO test_rows VALUES (1)"))
        if fail:
            raise RuntimeError("migration failed")
        return []

    with engine.connect() as connection:
        transaction = connection.begin()
        config.attributes["connection"] = connection
        with EnvironmentContext(config, script, fn=migrate):
            if fail:
                with pytest.raises(RuntimeError, match="migration failed"):
                    script.run_env()
            else:
                script.run_env()
        assert transaction.is_active
        assert not connection.closed
        transaction.rollback()
        assert connection.scalar(text("SELECT count(*) FROM test_rows")) == 0
    engine.dispose()
    factory.assert_not_called()
