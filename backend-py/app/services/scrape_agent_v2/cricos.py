"""Read the official CRICOS snapshot; never infer current tuition or admission."""
import json,re
from urllib.parse import urlencode
from datetime import datetime,timezone
from app.services.scrape_assistant_tools import public_request

RESOURCE="48cacf69-2082-415e-9595-f17d0c3a4af0"
BASE="https://data.gov.au/data/api/3/action/datastore_search"


def normalize(value):
    return re.sub(r"[^a-z0-9]","",str(value or "").casefold())


async def lookup(country,code,provider,course):
    if str(country).casefold() not in {"australia","au","aus"}:
        return {"ok":True,"status":"not_applicable","changed":False,"reason":"CRICOS applies only to Australian providers"}
    code=str(code or "").strip().upper()
    if not re.fullmatch(r"\d{6}[A-Z]",code):
        return {"ok":True,"status":"eligibility_unverified","changed":False,
                "reason":"No valid course CRICOS code; do not reject the course"}
    url=BASE+"?"+urlencode({"resource_id":RESOURCE,"limit":10,
                           "filters":json.dumps({"CRICOS Course Code":code})})
    try:
        payload=json.loads(await public_request(url,user_agent="StudyInfoCentre-ScrapeAssistant/2.0"))
        if payload.get("success") is not True:
            raise ValueError("The official registry did not return a successful result")
        data=payload["result"]
        records=data.get("records",[])
        if data.get("total",0)>10:
            raise ValueError("Registry result is incomplete")
        matches=[r for r in records if r.get("CRICOS Course Code")==code]
        selected=[r for r in matches if normalize(r.get("Institution Name"))==normalize(provider)
                  and normalize(r.get("Course Name"))==normalize(course)]
        active=[r for r in selected if str(r.get("Expired","")).casefold()=="no"]
        return {"ok":True,"changed":False,"status":"registration_matched" if active else "eligibility_unverified",
                "source":url,"checkedAt":datetime.now(timezone.utc).isoformat(),
                "matches":[{k:r.get(k) for k in ("CRICOS Provider Code","Institution Name","CRICOS Course Code","Course Name","Expired")} for r in matches],
                "reason":("Official register snapshot matches the institution and course. Current availability still needs course-page evidence."
                    if active else "No exact active institution/course match; review aliases, expired records or missing registration. Do not auto-reject."),
                "feeAuthority":False}
    except Exception as exc:
        return {"ok":False,"changed":False,"status":"eligibility_unverified","source":url,
                "reason":str(exc)[:250]}
