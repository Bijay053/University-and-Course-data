"""Access failures are not extraction diagnoses or permission to retry forever."""
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.services import assistant_source_cache as cache
from app.services import scrape_assistant_tools as tools
from app.services.assistant_source_gate import access_blocked_answer, gate_unreachable_repair

HOME = "https://www.lsbu.ac.uk"
URL = HOME + "/study/course-finder/law-llb-hons"


def denied():
    return {
        "url": URL, "verified": False, "sourceType": "html",
        "accessIssue": "robots_unavailable", "httpStatus": 403,
        "error": "robots.txt could not be verified (HTTP 403).",
    }


class Store:
    """Private in-memory session double, with the real SQL ownership predicates."""
    def __init__(self):
        self.thread = SimpleNamespace(
            id="thread", turn_id="turn", run_id="owner", status="researching",
            messages=[{"role": "user", "content": "why is the fee missing?"}],
        )
        self.commits = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, model, identity):
        return self.thread if identity == self.thread.id else None

    async def execute(self, statement):
        params = statement.compile().params
        owns = (
            params["id_1"] == self.thread.id
            and params["turn_id_1"] == self.thread.turn_id
            and params["run_id_1"] == self.thread.run_id
            and params["status_1"] == self.thread.status
        )
        return SimpleNamespace(scalar_one_or_none=lambda: self.thread if owns else None)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_robots_403_stops_before_course_request(monkeypatch):
    original = httpx.AsyncClient
    calls = []

    def reply(request):
        calls.append(str(request.url))
        return httpx.Response(403, text="Forbidden")

    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(reply)))
    result = await tools.inspect_source(URL, HOME)
    assert not result["verified"]
    assert result["accessIssue"] == "robots_unavailable"
    assert result["httpStatus"] == 403
    assert calls == [HOME + "/robots.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    denied(),
    {"url": URL, "verified": False, "error": "Missing document", "httpStatus": 404,
     "failureStage": "source_http"},
    {"url": URL, "verified": False, "error": "Document could not be parsed"},
])
async def test_followup_reuses_failure_and_fragment_does_not_trigger_fetch(monkeypatch, failure):
    store = Store()
    service = SimpleNamespace(AsyncSessionLocal=store)
    calls = []

    async def inspect(url, home):
        calls.append(url)
        return deepcopy(failure)

    monkeypatch.setattr(cache, "wall_time", lambda: 1000)
    args = (service, "thread", "turn", "owner", URL, HOME, inspect, 1)
    await cache.cached_source(*args)
    store.thread.messages.append({"role": "user", "content": "fix this"})
    result = await cache.cached_source(*args[:4], URL + "#fees", *args[5:])
    assert calls == [URL]
    assert result["_sourceCacheHit"]
    assert not result["verified"]
    assert store.commits == 1

    # Returned values cannot mutate the saved, private evidence.
    result["error"] = "changed by caller"
    again = await cache.cached_source(*args)
    assert again["error"] == failure["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh,elapsed", [(True, 1), (False, 121)])
async def test_expiry_or_explicit_fresh_request_rechecks(monkeypatch, fresh, elapsed):
    store = Store()
    service = SimpleNamespace(AsyncSessionLocal=store)
    calls = []

    async def inspect(url, home):
        calls.append(url)
        return denied()

    monkeypatch.setattr(cache, "wall_time", lambda: 1000)
    args = (service, "thread", "turn", "owner", URL, HOME, inspect, 1)
    await cache.cached_source(*args)
    monkeypatch.setattr(cache, "wall_time", lambda: 1000 + elapsed)
    await cache.cached_source(*args, fresh=fresh)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_failure_does_not_resurrect_old_verified_source(monkeypatch):
    store = Store()
    old = {"verified": True, "url": URL, "_sourceScopeVersion": 2}
    latest = {**denied(), "_sourceScopeVersion": 2, "_sourceCheckedAt": 1000}
    store.thread.messages = [
        {"role": "user", "_sourceChecks": {URL: old}},
        {"role": "user", "_sourceChecks": {URL: latest}},
    ]
    calls = []

    async def inspect(url, home):
        calls.append(url)
        return denied()

    monkeypatch.setattr(cache, "wall_time", lambda: 1121)
    result = await cache.cached_source(
        SimpleNamespace(AsyncSessionLocal=store), "thread", "turn", "owner",
        URL, HOME, inspect, 1,
    )
    assert calls == [URL] and not result["verified"]


@pytest.mark.asyncio
async def test_lost_owner_cannot_write_failure_cache():
    store = Store()
    store.thread.run_id = "replacement"

    async def inspect(url, home):
        return denied()

    await cache.cached_source(
        SimpleNamespace(AsyncSessionLocal=store), "thread", "turn", "owner",
        URL, HOME, inspect, 1,
    )
    assert store.commits == 0
    assert "_sourceChecks" not in store.thread.messages[0]


@pytest.mark.asyncio
async def test_cache_does_not_cross_conversations():
    store = Store()
    saved = {**denied(), "_sourceScopeVersion": 2, "_sourceCheckedAt": 1000}
    store.thread.messages[0]["_sourceChecks"] = {URL: saved}
    calls = []

    async def inspect(url, home):
        calls.append(url)
        return denied()

    await cache.cached_source(
        SimpleNamespace(AsyncSessionLocal=store), "other-thread", "turn", "owner",
        URL, HOME, inspect, 1,
    )
    assert calls == [URL] and store.commits == 0


def test_response_does_not_invent_cause_add_ielts_or_queue_work():
    result = gate_unreachable_repair({
        "content": "I will queue a fix.", "sources": [],
        "actions": [{"kind": "fix_fields", "state": "proposed"}],
    }, [denied()])
    assert "does not establish why" in result["content"]
    assert "staff-provided" in result["content"]
    assert "IELTS" not in result["content"]
    assert result["actions"][0]["state"] == "blocked"


def test_saved_access_failure_is_disclosed_and_valid_source_is_not_overridden():
    source = denied()
    source["_sourceCacheHit"] = True
    assert "no new fetch" in access_blocked_answer([source])
    assert access_blocked_answer([source, {"verified": True}]) is None
    assert access_blocked_answer([]) is None
    timeout = access_blocked_answer([{"verified": False, "error": "timeout"}])
    assert "does not establish why" in timeout
    assert "No wider course repair is promised" in timeout
    assert "access restrictions" not in timeout


@pytest.mark.asyncio
async def test_successful_evidence_reused_without_false_failure():
    store = Store()
    calls = []

    async def inspect(url, home):
        calls.append(url)
        return {"verified": True, "url": url, "text": "Official fee evidence", "excerpts": []}

    args = (SimpleNamespace(AsyncSessionLocal=store), "thread", "turn", "owner",
            URL, HOME, inspect, 1)
    first = await cache.cached_source(*args)
    saved = deepcopy(store.thread.messages)
    second = await cache.cached_source(*args)
    assert first["verified"] and second["verified"]
    assert second["_sourceCacheHit"] and calls == [URL]
    assert store.thread.messages == saved


@pytest.mark.parametrize("verified,minutes,expected", [
    (True, 60, True), (False, 1, True), (False, 3, False),
    (True, 1441, False), (False, -1, False),
])
def test_v2_failure_cache_is_short_lived(verified, minutes, expected):
    from datetime import datetime, timedelta, timezone
    from app.services.scrape_agent_v2.web import reusable_page
    checked = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    assert reusable_page({"verified": verified, "checkedAt": checked.isoformat()}) is expected
    assert not reusable_page({"checkedAt": "invalid"})


@pytest.mark.asyncio
async def test_v2_preserves_robots_failure_and_rechecks_after_expiry(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.services.scrape_agent_v2 import web
    store = Store()
    calls = []
    agent = SimpleNamespace(
        ctx={"home": HOME}, service=SimpleNamespace(AsyncSessionLocal=store),
        thread_id="thread", turn_id="turn", run_id="owner",
    )

    async def throttle(host):
        pass

    async def request(url, **kwargs):
        calls.append(url)
        raise tools.SourceReadError("Official source refused access (HTTP 403).",
                                    access_issue="source_access_denied", http_status=403)

    monkeypatch.setattr(web, "throttle", throttle)
    monkeypatch.setattr(web, "public_request", request)
    first = await web.web_tool(agent, "fetch_page", {"url": URL})
    assert first["accessIssue"] == "robots_unavailable" and not first["verified"]
    assert calls == [HOME + "/robots.txt"]
    second = await web.web_tool(agent, "fetch_page", {"url": URL})
    assert second["cached"] and calls == [HOME + "/robots.txt"]
    store.thread.messages[-1]["_agentV2Pages"][URL + "|static|"]["checkedAt"] = (
        datetime.now(timezone.utc) - timedelta(minutes=3)
    ).isoformat()
    await web.web_tool(agent, "fetch_page", {"url": URL})
    assert calls == [HOME + "/robots.txt", HOME + "/robots.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_status", [404, 410])
async def test_v2_missing_robots_is_not_a_denial(monkeypatch, missing_status):
    from app.services.scrape_agent_v2 import web
    store = Store()
    calls = []
    agent = SimpleNamespace(
        ctx={"home": HOME}, service=SimpleNamespace(AsyncSessionLocal=store),
        thread_id="thread", turn_id="turn", run_id="owner",
    )

    async def throttle(host):
        pass

    async def request(url, **kwargs):
        calls.append(url)
        if url.endswith("/robots.txt"):
            raise tools.SourceReadError("Official source returned missing robots.", http_status=missing_status)
        return b"<html><main>Official course</main></html>"

    async def inspect(url, home, *, raw_body):
        assert raw_body
        return {"verified": True, "url": url, "text": "Official course"}

    monkeypatch.setattr(web, "throttle", throttle)
    monkeypatch.setattr(web, "public_request", request)
    monkeypatch.setattr(web, "inspect_source", inspect)
    result = await web.web_tool(agent, "fetch_page", {"url": URL})
    assert result["verified"]
    assert calls == [HOME + "/robots.txt", URL]
