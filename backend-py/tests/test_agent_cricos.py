import json
import pytest
from app.services.scrape_agent_v2 import cricos


@pytest.mark.asyncio
async def test_missing_code_is_unverified_not_rejected_and_uk_is_not_applicable(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError("No fetch is needed")
    monkeypatch.setattr(cricos,"public_request",forbidden)
    assert (await cricos.lookup("Australia",None,"Excelsia","Course"))["status"]=="eligibility_unverified"
    assert (await cricos.lookup("UK",None,"University","Course"))["status"]=="not_applicable"


@pytest.mark.asyncio
async def test_registry_identity_and_expiry_boundaries(monkeypatch):
    row={"CRICOS Course Code":"078241E","Institution Name":"Canberra Institute of Technology",
         "Course Name":"ACT Year 12 Certificate","Expired":"No"}
    async def source(*args,**kwargs):
        return json.dumps({"success":True,"result":{"total":1,"records":[row]}}).encode()
    monkeypatch.setattr(cricos,"public_request",source)
    exact=await cricos.lookup("Australia","078241E",row["Institution Name"],row["Course Name"])
    assert exact["status"]=="registration_matched" and not exact["feeAuthority"]
    wrong=await cricos.lookup("Australia","078241E","Different University",row["Course Name"])
    assert wrong["status"]=="eligibility_unverified"
    row["Expired"]="Yes"
    assert (await cricos.lookup("Australia","078241E",row["Institution Name"],row["Course Name"]))["status"]=="eligibility_unverified"
