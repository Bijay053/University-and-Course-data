from __future__ import annotations

import pytest

from app.main import app


@pytest.fixture
def fastapi_app():
    return app
