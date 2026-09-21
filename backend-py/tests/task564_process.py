"""Child-process entrypoint for the disposable task-564 acceptance harness.

Only the official HTTP boundary is substituted. The router, permission checks,
Celery task, extraction, staging and workflow monitor are production code.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

HOST = "task564.example.test"
SOURCE = "task564-source"


def isolate_recipe_files():
    """Keep production defaults readable but every generated recipe private."""
    from app.services.scraper.config import loader
    from app.services import scraper_config_ai
    from app.services.scraper import yaml_cascade

    root = Path(os.environ["TASK564_RECIPE_ROOT"]).resolve()
    assert root.parent.name.startswith("task564-private-")
    root.mkdir(parents=True, exist_ok=True)
    (root / "unis").mkdir(exist_ok=True)
    (root / "runtime_unis").mkdir(exist_ok=True)
    loader._UNIS_DIR = root / "unis"
    loader._RUNTIME_UNIS_DIR = root / "runtime_unis"
    scraper_config_ai._UNIS_DIR = root / "unis"
    yaml_cascade._UNIS_DIR = root / "unis"
    # Defaults/template paths remain production paths, for read-only use.
    # Catch any future hardcoded recipe writer instead of silently leaking.
    protected = (Path(__file__).parents[1] / "scraper_config").resolve()

    def protect(event, args):
        paths = []
        if event == "open":
            name, mode, flags = args
            if ((isinstance(mode, str) and any(char in mode for char in "wax+"))
                    or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                paths = [name]
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            paths = [args[0]]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            paths = list(args[:2])
        for name in paths:
            if isinstance(name, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(name)).resolve()
                if path == protected or protected in path.parents:
                    with (root / "forbidden-writes.txt").open("a") as log:
                        log.write(f"{event}: {path}\n")
                    raise PermissionError(f"Task564 production recipe tree is read-only: {path}")

    sys.addaudithook(protect)


def install_http_fixture():
    """Map the reserved official fixture host to a real loopback HTTP server."""
    import httpx
    from app.services import scraper_config_ai

    original_safe = scraper_config_ai._is_safe_public_url
    original_send = httpx.AsyncClient.send

    def safe(url):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname == HOST:
            return True, "Explicit isolated acceptance fixture"
        return original_safe(url)

    async def send(client, request, **kwargs):
        if request.url.host == HOST:
            request.url = request.url.copy_with(
                scheme="http", host="127.0.0.1",
                port=int(os.environ["TASK564_HTTP_PORT"]),
            )
        elif request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(f"Task564 forbids external HTTP: {request.url.host}")
        return await original_send(client, request, **kwargs)

    scraper_config_ai._is_safe_public_url = safe
    httpx.AsyncClient.send = send


async def seed():
    from sqlalchemy import text
    from app.database import Base, engine, AsyncSessionLocal
    from app.models import University, ScrapeRuntimeJob, ScrapedCourse
    from app.models.user import User
    from app.routers.auth import hash_password
    from scripts.apply_migration_047 import STATEMENTS as AUDIT_MIGRATION
    from scripts.apply_migration_040 import run as migrate_taxonomy

    async with engine.begin() as conn:
        # Raw workflow SQL relies on migration-owned NOW() server defaults;
        # the ORM model has Python defaults instead. Build this table from its
        # actual production migration, not a hand-edited test approximation.
        await conn.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[table for table in Base.metadata.sorted_tables
                          if table.name != "ai_repair_audits"],
        ))
        for statement in AUDIT_MIGRATION:
            await conn.execute(text(statement))
        for filename in ("382_autonomous_worker_claims.py", "383_worker_claim_lock_lineage.py"):
            path = Path(__file__).parents[1] / "alembic/versions" / filename
            spec = importlib.util.spec_from_file_location(filename, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            def migrate(sync):
                module.op = SimpleNamespace(execute=lambda sql: sync.execute(text(sql)))
                module.upgrade()
            await conn.run_sync(migrate)
    # Actual vocabulary migration creates and seeds course_sub_categories,
    # which is intentionally not part of app.models' default import registry.
    await migrate_taxonomy()
    async with AsyncSessionLocal() as db:
        uni = University(
            name="Task564 Isolated University", country="Australia", city="Sydney",
            website=f"https://{HOST}", scrape_url=f"https://{HOST}/courses",
            scrape_config={"requires_browser": False},
        )
        db.add(uni)
        db.add(User(email="task564@example.test", full_name="Task564 Tester",
                    password_hash=hash_password("isolated-task564-password"),
                    is_active=True, is_super_admin=True))
        await db.flush()
        db.add(ScrapeRuntimeJob(
            runtime_job_id=SOURCE, university_id=uni.id, university_name=uni.name,
            url=uni.scrape_url, job_type="scrape", status="completed",
            imported=1, current=1, total_found=1,
            gate_skip_counts={"non_degree": 1},
        ))
        db.add(ScrapedCourse(
            scrape_job_id=SOURCE, university_id=uni.id,
            course_name="Bachelor of Existing Evidence", status="pending",
            course_website=f"https://{HOST}/courses/existing",
            canonical_course_url=f"https://{HOST}/courses/existing",
        ))
        capped_uni = University(
            name="Task564 Count Cap University", country="Australia", city="Sydney",
            website=f"https://{HOST}", scrape_url=f"https://{HOST}/bounded-catalogue",
            scrape_config={"requires_browser": False},
        )
        db.add(capped_uni)
        await db.flush()
        db.add(ScrapeRuntimeJob(
            runtime_job_id="task564-cap-source", university_id=capped_uni.id,
            university_name=capped_uni.name, url=capped_uni.scrape_url,
            job_type="scrape", status="completed", imported=1, current=1, total_found=1,
        ))
        db.add(ScrapedCourse(
            scrape_job_id="task564-cap-source", university_id=capped_uni.id,
            course_name="Bachelor of Preserved Cap Evidence", status="pending",
            course_website=f"https://{HOST}/bounded-existing",
        ))
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    # Refuse accidental direct execution against inherited/shared infrastructure.
    assert os.environ.get("TASK564_ISOLATED") == "yes"
    assert "@127.0.0.1:" in os.environ["DATABASE_URL"]
    assert os.environ["REDIS_URL"].startswith("redis://127.0.0.1:")
    isolate_recipe_files()
    install_http_fixture()
    if sys.argv[1] == "seed":
        asyncio.run(seed())
    elif sys.argv[1] == "api":
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1",
                    port=int(os.environ["TASK564_API_PORT"]))
    elif sys.argv[1] == "worker":
        from app.tasks.celery_app import celery_app
        celery_app.worker_main([
            "worker", "--pool=prefork", "--concurrency=1", "--loglevel=INFO",
            "--without-gossip", "--without-mingle", "--without-heartbeat",
            "-Q", "scrape", "-n", "task564@%h",
        ])
    else:
        raise SystemExit("Expected seed, api or worker")