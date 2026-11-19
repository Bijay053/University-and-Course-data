from types import SimpleNamespace
from copy import deepcopy
import pytest
from app.services.assistant_pending_automation import prepare, supported
from app.services.scrape_assistant_autorun import choose_action
from app.services.scrape_agent_v2.repair import proposal

PROMPT = "fix this scrapying bug i need all course data"
SETTINGS = SimpleNamespace(scrape_assistant_auto_pending_user_ids="1")


def repair():
    tools = SimpleNamespace(prompt=PROMPT, turn_id="new-turn",
                            service=SimpleNamespace(fingerprint=lambda _: "config-version"))
    return proposal(tools, SimpleNamespace(scrape_config={}),
                    {"repairAvailable": True}, "call")["proposal"]


@pytest.mark.parametrize("actor,prompt,auto,expected", [
    (1, PROMPT, True, True),
    (2, PROMPT, True, False),
    (1, PROMPT, False, False),
    (1, "Only explain the scraper failure", True, False),
    (1, "Do not run this repair", True, False),
    (1, "Show a preview of the scraper repair", True, False),
    (1, "Read-only: investigate Acadia", True, False),
])
def test_real_provider_proposal_respects_account_and_request(actor, prompt, auto, expected):
    action = repair()
    original = deepcopy(action)
    reply = {"content": (
        "The test can select up to 50 courses, so batch approval is required. "
        "No repair has run yet and no course has been published."), "actions": [action]}
    prepare(reply, {"content": prompt, "autoRun": auto}, SETTINGS, actor)
    assert (choose_action([action], prompt) is action) == expected
    assert bool(action.get("automaticPending")) == expected
    assert action["requiresConfirmation"] is not expected
    assert action["_configVersion"] == original["_configVersion"]
    assert action["_boundedRepair"] == original["_boundedRepair"]
    assert ("batch approval is required" in reply["content"]) is not expected
    if expected:
        assert "Execution results will appear here" in reply["content"]


@pytest.mark.parametrize("change", [
    {"courseCount": 51}, {"courseCount": 0}, {"courseCount": True},
    {"_configVersion": ""}, {"_boundedRepair": None},
    {"_boundedRepair": {"maxVerificationCourses": 50, "publishesCourses": True}},
    {"_boundedRepair": {"maxVerificationCourses": 500, "publishesCourses": False}},
    {"publishedCount": 1}, {"state": "running"}, {"state": "error"},
    {"_confirmationOnly": True}, {"conflicts": ["unsafe"]},
])
def test_unbounded_legacy_published_and_terminal_proposals_stay_gated(change):
    action = {**repair(), **change}
    assert not supported(action)
    prepare({"actions": [action]}, {"content": PROMPT, "autoRun": True}, SETTINGS, 1)
    assert not action.get("automaticPending")
