from copy import deepcopy

import pytest

from app.services.assistant_repair_proposals import finalize_field_proposal, research_only
from tests.test_cached_ielts_recovery import URL, TEXT


def fixture():
    rows=[{"id":1,"name":"Physiotherapy","url":URL,"ielts":6.5,"ieltsBands":{}},
          {"id":2,"name":"Nursing","url":URL+"/nursing","ielts":7,"ieltsBands":{}}]
    source={"url":URL,"verified":True,"text":TEXT,"facts":[{
        "field":"ielts_overall","quote":TEXT,
        "normalized":{"ielts_overall":6.5, **{"ielts_"+p:6 for p in
                      ("listening","reading","writing","speaking")}}}]}
    action={"kind":"fix_fields","fields":["ielts_overall"],"_ids":[1],"courseCount":139}
    return action,{"samples":rows},[source]


@pytest.mark.parametrize("prompt", [
    "why each band IELTS is missing", "Only investigate IELTS and prepare a proposal",
    "Show the IELTS repair", "Prepare an IELTS preview", "IELTS bands missing",
    "Do not apply changes; fix IELTS in a preview",
])
def test_research_is_not_write_permission(prompt):
    assert research_only(prompt)


def test_actual_scope_and_all_five_values_override_model_claim():
    action,ctx,sources=fixture()
    answer=finalize_field_proposal(action,"why IELTS bands missing",ctx,sources)
    assert action["courseCount"]==1
    assert len(action["courseScope"])==1
    assert "exactly 1" in answer and "1 are not included" in answer
    assert action["requiresConfirmation"]
    changes=action["repairPreview"][0]["changes"]
    assert len(changes)==5
    assert changes[0]=={"field":"ielts_overall","oldValue":6.5,"newValue":6.5}
    assert all(c["oldValue"] is None and c["newValue"]==6 for c in changes[1:])


@pytest.mark.parametrize("variation", ["pdf","unverified","foreign","bad_quote","incomplete","conflict","bool"])
def test_unclear_or_unowned_evidence_never_proposes_numbers(variation):
    action,ctx,sources=fixture()
    if variation=="pdf": sources[0]["sourceType"]="pdf"
    if variation=="unverified": sources[0]["verified"]=False
    if variation=="foreign": sources[0]["url"]+="/unrelated"
    if variation=="bad_quote": sources[0]["facts"][0]["quote"]="IELTS quote invented without source"
    if variation=="incomplete": del sources[0]["facts"][0]["normalized"]["ielts_reading"]
    if variation=="bool": sources[0]["facts"][0]["normalized"]["ielts_reading"]=True
    if variation=="conflict":
        conflicting=deepcopy(sources[0]["facts"][0])
        conflicting["normalized"]["ielts_speaking"]=7
        sources[0]["facts"].append(conflicting)
    finalize_field_proposal(action,"Fix IELTS",ctx,sources)
    assert action["requiresConfirmation"]
    assert action["repairPreview"][0]["status"]=="unverified"
    assert action["repairPreview"][0]["changes"]==[]


def test_one_course_is_not_evidence_for_the_other_selected_course():
    action,ctx,sources=fixture()
    action["_ids"]=[1,2]
    finalize_field_proposal(action,"Fix IELTS",ctx,sources)
    assert [p["status"] for p in action["repairPreview"]]==["supported","unverified"]
    assert action["requiresConfirmation"]


def test_clear_small_explicit_repair_still_allows_pending_automation():
    action,ctx,sources=fixture()
    finalize_field_proposal(action,"Fix IELTS",ctx,sources)
    assert not action["requiresConfirmation"]


def test_large_batch_requires_confirmation_and_unknown_ids_fail_closed():
    action,ctx,sources=fixture()
    ctx["samples"]*=6
    ctx["samples"]=[{**r,"id":i} for i,r in enumerate(ctx["samples"],1)]
    action["_ids"]=[r["id"] for r in ctx["samples"]]
    finalize_field_proposal(action,"Fix IELTS",ctx,sources)
    assert action["requiresConfirmation"] and action["courseCount"]==12
    action["_ids"]=[9000]
    with pytest.raises(ValueError,match="unknown course"):
        finalize_field_proposal(action,"Fix IELTS",ctx,sources)
