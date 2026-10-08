"""Safety checks for child-process-only validation broker redirection."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import redis.connection
import redis.asyncio.connection

from tests.helpers.run_disposable_redis import redirect_child_connections


def install_redirect(monkeypatch):
    sync = Mock(return_value="private-socket")
    async_ = AsyncMock(return_value="private-reader-writer")
    monkeypatch.setattr(redis.connection.Connection, "_connect", sync)
    monkeypatch.setattr(redis.asyncio.connection.Connection, "_connect", async_)
    redirect_child_connections(45678)
    return sync, async_


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_shared_sync_address_redirected_before_connection(monkeypatch, host):
    sync, _ = install_redirect(monkeypatch)
    connection = SimpleNamespace(host=host, port=6379, username="unused", password="unused")
    assert redis.connection.Connection._connect(connection) == "private-socket"
    assert (connection.host, connection.port) == ("127.0.0.1", 45678)
    assert connection.username is None and connection.password is None
    sync.assert_called_once_with(connection)


@pytest.mark.asyncio
async def test_shared_async_address_redirected_before_connection(monkeypatch):
    _, async_ = install_redirect(monkeypatch)
    connection = SimpleNamespace(host="localhost", port=6379, username=None, password=None)
    assert await redis.asyncio.connection.Connection._connect(connection) == "private-reader-writer"
    assert (connection.host, connection.port) == ("127.0.0.1", 45678)
    async_.assert_awaited_once_with(connection)


def test_other_private_brokers_are_not_redirected(monkeypatch):
    sync, _ = install_redirect(monkeypatch)
    connection = SimpleNamespace(host="127.0.0.1", port=34567)
    redis.connection.Connection._connect(connection)
    assert connection.port == 34567
    sync.assert_called_once_with(connection)


@pytest.mark.asyncio
async def test_remote_brokers_fail_closed_before_connection(monkeypatch):
    sync, async_ = install_redirect(monkeypatch)
    connection = SimpleNamespace(host="not-a-local-broker.test", port=6379)
    with pytest.raises(RuntimeError, match="forbids remote Redis"):
        redis.connection.Connection._connect(connection)
    with pytest.raises(RuntimeError, match="forbids remote Redis"):
        await redis.asyncio.connection.Connection._connect(connection)
    sync.assert_not_called()
    async_.assert_not_awaited()


def test_disposable_broker_smoke_and_cleanup():
    runner = Path(__file__).parent / "helpers" / "run_disposable_redis.py"
    result = subprocess.run(
        [sys.executable, str(runner), "--smoke"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Private synchronous and asynchronous Redis connections passed." in result.stdout
    assert '"disposable_redis_stopped": true' in result.stdout
