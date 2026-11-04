"""Loopback-only real API process for disposable restart/fencing tests.

Never imports the production app/lifespan. Database and network boundaries are
installed before importing the actual assistant router and local runner.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--block", choices=("source", "answer", "none"), default="none")
    args = parser.parse_args()
    assert Path(args.socket).resolve().name.startswith("hsu-pg-")
    assert re.fullmatch(r"assistant_process_[a-f0-9]{32}", args.schema)
    # No connector credentials or normal application DB URL are used.
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://hsu_test@/postgres?host=" + quote(args.socket, safe="")
    os.environ["DATABASE_REQUIRE_TLS"] = "false"
    from app import database

    engine = create_async_engine(
        "postgresql+asyncpg://", poolclass=NullPool,
        connect_args={"host": args.socket, "user": "hsu_test", "database": "postgres",
                      "server_settings": {"search_path": args.schema}},
        execution_options={"schema_translate_map": {None: args.schema, "public": args.schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.AsyncSessionLocal = engine, factory
    from app.routers import scrape_assistant as api
    from app.services import scrape_assistant as service, scrape_assistant_runner as runner

    directory = Path(args.directory)

    async def barrier(stage):
        if args.block != stage:
            return
        (directory / "entered").write_text(stage)
        try:
            while not (directory / "release").exists():
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            (directory / "cancelled").write_text(stage)
            raise

    async def source(url, home):
        await barrier("source")
        return {"url": url, "title": "Official source", "verified": True,
                "text": "International tuition is £18,900 per year.",
                "facts": [], "excerpts": [{"id": "q0", "text": "International tuition is £18,900 per year."}]}

    async def answer(**kwargs):
        await barrier("answer")
        # Even a proposed correction must not cause a data write for a read-only
        # prompt. Leave the real automatic-action policy in place.
        return {"answer": f"Source-backed answer from {args.label}.",
                "citations": [{"url": "https://www.example.edu/course", "quoteId": "q0"}],
                "action": {"kind": "fix_fields", "fields": ["international_fee"]}}

    async def unexpected_catalogue(*args, **kwargs):
        raise AssertionError("Unexpected network catalogue access")

    service.inspect_source, service.chat_json = source, answer
    service.inspect_catalogue = unexpected_catalogue

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await runner.shutdown()
            assert not runner._tasks
            await engine.dispose()
            (directory / "shutdown").write_text("runner drained")

    app = FastAPI(lifespan=lifespan)

    async def get_test_db():
        async with factory() as db:
            yield db

    async def actor():
        return {"id": 1}

    app.dependency_overrides[database.get_db] = get_test_db
    app.dependency_overrides[api.Actor.__metadata__[0].dependency] = actor
    app.include_router(api.router, prefix="/api/scrape")

    @app.get("/__ready")
    async def ready():
        return {"pid": os.getpid(), "schema": args.schema, "tasks": len(runner._tasks)}

    @app.post("/__late_activity")
    async def late_activity(body: dict):
        try:
            await service.record_activity(
                body["thread"], body["turn"], body["owner"],
                service.event("Late old-owner event"),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": True}

    uvicorn.run(app, fd=args.fd, log_level="warning", timeout_graceful_shutdown=8)


if __name__ == "__main__":
    main()
