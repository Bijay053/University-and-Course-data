from __future__ import annotations

import pytest

from app.main import app
from app.services.scraper.config.context import current_uni_config


@pytest.fixture
def fastapi_app():
    return app


@pytest.fixture(autouse=True)
def _isolate_uni_config():
    """Prevent one scraper test's ContextVar config leaking into the next."""
    current_uni_config.set(None)
    yield
    current_uni_config.set(None)
