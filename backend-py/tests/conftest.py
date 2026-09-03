from __future__ import annotations

import pytest

from app.main import app
from app.services.scraper.config.context import current_uni_config


@pytest.fixture
def require_real_redis():
    """Fail clearly when an integration test's local Redis is unavailable."""
    import redis

    client = redis.from_url(
        "redis://127.0.0.1:6379",
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.fail(
            "Redis integration-test dependency is unavailable at "
            f"127.0.0.1:6379 ({exc}). Start Redis before running this test."
        )
    finally:
        client.close()


@pytest.fixture
def fastapi_app():
    return app


@pytest.fixture(autouse=True)
def _isolate_uni_config():
    """Prevent one scraper test's ContextVar config leaking into the next."""
    current_uni_config.set(None)
    yield
    current_uni_config.set(None)
