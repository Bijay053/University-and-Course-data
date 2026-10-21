"""Exercise the actual nested emit functions without starting a scrape."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


def functions(namespace):
    source = Path(__file__).parents[1] / "app/services/scraper/orchestrator.py"
    tree = ast.parse(source.read_text())
    flush = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_flush_emit_buf")
    emit = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "emit"
                and any(isinstance(x, ast.Name) and x.id == "_emit_buf" for x in ast.walk(n)))
    exec(compile(ast.Module(body=[flush, emit], type_ignores=[]), str(source), "exec"), namespace)
    return namespace


@pytest.mark.asyncio
async def test_cancelled_write_retains_done_record_for_final_drain():
    entered = asyncio.Event()
    release = asyncio.Event()
    session = Mock(commit=AsyncMock())
    async def execute(*args):
        entered.set()
        await release.wait()
    session.execute = AsyncMock(side_effect=execute)
    @asynccontextmanager
    async def db():
        yield session
    rows = [{"rid": "job", "seq": 1, "ev": "done", "pl": "{}"}]
    ns = functions({"_emit_buf": rows, "_emit_flush_lock": asyncio.Lock(),
                    "AsyncSessionLocal": db, "log": Mock()})
    task = asyncio.create_task(ns["_flush_emit_buf"]())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(rows) == 1
    session.commit.assert_not_awaited()
    release.set()
    await ns["_flush_emit_buf"]()
    assert rows == []
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_done_is_flushed_immediately_not_left_for_background_cleanup():
    ns = functions({"_emit_buf": [], "_seq": [1], "_EMIT_FLUSH_BATCH": 30,
                    "runtime_job_id": "job", "infer_log_level": lambda _: "info"})
    flush = ns["_flush_emit_buf"] = AsyncMock()
    await ns["emit"]("done", "Finished", imported=0, skipped=1)
    flush.assert_awaited_once()
    assert ns["_emit_buf"][0]["ev"] == "done"
