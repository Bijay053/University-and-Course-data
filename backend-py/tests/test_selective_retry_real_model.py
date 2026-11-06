"""Opt-in real-model routing probe; USD reservation uses the real shared ledger.

Only fake university rows live in the socket-only test database. No real course
or scrape job is changed, and no source fetch or extraction is authorized.
"""
import json
import os
from pathlib import Path
import pytest
from app.database import AsyncSessionLocal as budget_factory
from app.config import settings
from app.services import scrape_assistant as service
from app.services.scrape_agent_v2.model import complete
from app.services.scrape_agent_v2.tools import DataTools
from tests.test_selective_retry import seed, URL
from tests.test_scrape_prompt_assistant import store, assistant_postgres


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("RUN_SELECTIVE_RETRY_REAL_MODEL") != "1", reason="Opt-in real model; shared daily USD budget applies")
async def test_real_model_chooses_failure_ledger_and_reads_only_current_job(store, monkeypatch):
    from app.services.ai.openai_client import _client
    await seed(store, monkeypatch)
    prompt = "List the settled failed URLs and reserved attempt counts for this exact job. Do not retry or fetch pages yet."
    tools = DataTools(service, "eligibility", "turn", "owner",
        {"job": {"id": "source"}, "university_id": 1}, prompt)
    schemas = [s for s in tools.schemas if s["function"]["name"] in {"list_failed_urls", "retry_failed", "fetch_page"}]
    client = _client()
    try:
        response = await complete(client, [
            {"role": "system", "content": "Inspect exact current-job failure provenance with the database tool before considering retries. Respect the user's read-only request. Return one tool call only."},
            {"role": "user", "content": prompt},
        ], schemas, spend_context=(budget_factory, settings, "selective-retry-protocol-probe"))
        calls = response["message"].get("tool_calls", [])
        assert len(calls) == 1 and calls[0]["function"]["name"] == "list_failed_urls"
        result = await tools.execute(calls[0]["function"]["name"],
            json.loads(calls[0]["function"]["arguments"]), calls[0]["id"])
        assert result["ok"] and result["changed"] is False
        assert [f["url"] for f in result["failures"]] == [URL]
        report = {"passed": True, "kind": "real model plus private-database tool execution",
                  "tool": "list_failed_urls", "tokens": response["tokens"],
                  "realCourseWrites": 0, "sourceFetches": 0, "retryJobsQueued": 0,
                  "budget": "shared Sydney-day USD ledger reserved before request"}
        Path("../.local/runtime-proofs/selective-retry-real-model.json").write_text(json.dumps(report))
    finally:
        await client.close()
