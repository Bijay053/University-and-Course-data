"""B17: import-smoke for the Celery worker.

The B17 incident: ``app/tasks/snapshot_tasks.py`` imported ``psycopg2`` at
module top-level, but the project ships only asyncpg in requirements.txt.
Celery's worker boot does ``from app.tasks.snapshot_tasks import *`` while
processing the ``include=[...]`` list, which raised ``ModuleNotFoundError``
50+ times in prod before someone added ``psycopg2-binary`` by hand.

The unit test suite never caught it because no test imported the celery
side of the codebase. This module fills that gap: it forces an import of
``celery_app`` AND every entry in its ``include=[...]`` list, then
materialises ``celery_app.tasks`` so any module-scope crash in a task file
fails CI instead of the prod worker.
"""
from __future__ import annotations

import importlib

import pytest


def test_celery_app_imports_clean() -> None:
    """``import app.tasks.celery_app`` must succeed with only the
    declared requirements installed. Failing this means a task module is
    pulling in something that isn't in requirements.txt — exactly the
    B17 failure mode."""
    mod = importlib.import_module("app.tasks.celery_app")
    assert hasattr(mod, "celery_app"), "celery_app symbol missing"


def test_all_included_task_modules_import() -> None:
    """Every module listed in ``celery_app.conf.include`` must import
    cleanly. Celery's worker walks this list at boot — a single failing
    module brings the whole worker down (B17 root cause)."""
    from app.tasks.celery_app import celery_app

    include = list(celery_app.conf.include or [])
    assert include, "celery_app.include is empty — tasks would never register"
    for dotted in include:
        try:
            importlib.import_module(dotted)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"Celery worker boot would fail on `{dotted}`: "
                f"{type(exc).__name__}: {exc}"
            )


def test_worker_config_instantiates() -> None:
    """Materialise the worker-side bits of the Celery config so missing
    schedule entries / typoed queue names blow up here, not at 03:00 UTC.

    We exercise:
      * ``conf.beat_schedule`` shape — daily snapshot must reference the
        canonical ``tasks.snapshot.editable`` task name
      * ``celery_app.tasks`` — forces task discovery for every included
        module (this is what surfaces module-level import errors that a
        plain ``import`` would miss if the module is lazily loaded)
    """
    from app.tasks.celery_app import celery_app

    # Force task discovery (this is what worker boot does).
    celery_app.loader.import_default_modules()

    registered = {
        name for name in celery_app.tasks
        if not name.startswith("celery.")
    }
    assert "tasks.snapshot.editable" in registered, (
        f"snapshot task not registered. Got: {sorted(registered)}"
    )
    assert "scrape.university" in registered, (
        f"scrape task not registered. Got: {sorted(registered)}"
    )

    # Beat schedule sanity — the daily snapshot must point at the real
    # task name. A typo here would silently disable the backup until
    # someone notices the missing rows.
    schedule = celery_app.conf.beat_schedule or {}
    snap_entry = schedule.get("snapshot-editable-tables-daily")
    assert snap_entry is not None, "snapshot-editable-tables-daily missing from beat_schedule"
    assert snap_entry["task"] == "tasks.snapshot.editable"


def test_snapshot_tasks_does_not_import_psycopg2() -> None:
    """B17 regression: ``snapshot_tasks`` must not pull in psycopg2.

    The project ships only asyncpg; reintroducing psycopg2 (even as a
    convenience for a sync helper) reintroduces the prod crash loop.
    """
    import sys
    # Re-import fresh so the assertion reflects current code, not
    # whatever a previous test happened to leave in sys.modules.
    sys.modules.pop("app.tasks.snapshot_tasks", None)
    sys.modules.pop("psycopg2", None)
    importlib.import_module("app.tasks.snapshot_tasks")
    assert "psycopg2" not in sys.modules, (
        "snapshot_tasks pulled in psycopg2 — prod doesn't have it. "
        "Use the asyncpg AsyncSession pattern instead (see scrape_tasks.py)."
    )
