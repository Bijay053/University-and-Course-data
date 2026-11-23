"""A requested approval preview does not depend on model action selection."""
from copy import deepcopy
import pytest
from app.services import scrape_assistant as service
from app.services.assistant_repair_proposals import propose_verified_ielts_preview
from app.services.scrape_assistant_autorun import execution_requested, choose_action
from tests.test_assistant_repair_proposals import fixture

PROMPT = (
    "Verify IELTS overall and each band for all courses and prepare a correction preview only. "
    "Do not apply changes, approve or publish courses, or run catalogue discovery."
)

def data():
    _,ctx,sources=fixture()
    ctx.update(eligible_ids=[1,2], versions={"1":"v1","2":"v2"},config_version="config1")
    return ctx,sources

def test_explicit_preview_has_all_five_values_and_only_proven_eligible_rows():
    ctx,sources=data()
    before=deepcopy((ctx,sources))
    action=propose_verified_ielts_preview(service,PROMPT,ctx,sources)
    assert action["kind"]=="fix_fields"
    assert action["_ids"]==[1] and action["courseCount"]==1
    assert [p["id"] for p in action["repairPreview"]]==[1]
    assert len(action["repairPreview"][0]["changes"])==5
    assert action["_versions"]==ctx["versions"]
    assert action["_configVersion"]==ctx["config_version"]
    assert action["_supportingSources"][0]["excerpt"]==sources[0]["text"]
    assert action["requiresConfirmation"]
    assert not execution_requested(PROMPT)
    assert choose_action([action],PROMPT) is None
    assert (ctx,sources)==before

@pytest.mark.parametrize("prompt",[
    "Fix all missing IELTS bands", "Why is IELTS missing?",
    "Prepare a fee preview only", "Read-only IELTS investigation.",
])
def test_no_unrequested_proposal_or_change_of_target_field(prompt):
    ctx,sources=data()
    assert propose_verified_ielts_preview(service,prompt,ctx,sources) is None

@pytest.mark.parametrize("variation",[
    "unverified","pdf","wrong_url","conflict","incomplete","ineligible",
])
def test_unsupported_conflicting_or_ineligible_rows_get_no_executable_scope(variation):
    ctx,sources=data()
    if variation=="unverified": sources[0]["verified"]=False
    if variation=="pdf": sources[0]["sourceType"]="pdf"
    if variation=="wrong_url": sources[0]["url"]+="/another"
    if variation=="incomplete": del sources[0]["facts"][0]["normalized"]["ielts_writing"]
    if variation=="ineligible": ctx["eligible_ids"]=[2]
    if variation=="conflict":
        fact=deepcopy(sources[0]["facts"][0])
        fact["normalized"]["ielts_writing"]=7
        sources[0]["facts"].append(fact)
    assert propose_verified_ielts_preview(service,PROMPT,ctx,sources) is None

def test_already_verified_unchanged_requirements_do_not_generate_repair_work():
    ctx,sources=data()
    ctx["samples"][0].update(englishVerification="verified",
        ieltsBands={key:6 for key in ("reading","writing","speaking","listening")})
    assert propose_verified_ielts_preview(service,PROMPT,ctx,sources) is None
