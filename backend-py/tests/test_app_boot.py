"""Smoke tests that don't need a live DB — verify imports + route registration."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_app_imports():
    assert app.title.startswith("University Portal")


def test_health_route_registered():
    paths = {r.path for r in app.routes}
    assert "/api/health" in paths
    assert "/api/auth/login" in paths
    assert "/api/universities" in paths


def test_health_endpoint():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
