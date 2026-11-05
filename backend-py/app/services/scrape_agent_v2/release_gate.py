"""Fail-closed benchmark readiness. No synthetic rows count as verified courses."""
from collections import Counter
from pathlib import Path
import json


def evaluate(manifest,golden,observations,scenario_results,security_passed):
    failures=[]
    ids={r["id"] for r in manifest["requirements"]}
    passed={r["id"] for r in scenario_results if r.get("outcome")=="passed"}
    missing=sorted(ids-passed)
    if missing:
        failures.append(f"{len(missing)} required scenario/country checks have not passed")
    verified=[r for r in golden if r.get("review_method")=="independent_official_source_review"
              and r.get("reviewed_by") and r.get("checked_at") and r.get("fields")
              and any(f.get("status")=="verified" and f.get("source_url") and f.get("snippet")
                      for f in r["fields"].values())]
    if len({r["id"] for r in verified})<50:
        failures.append("Fewer than 50 independently checked golden courses")
    if not {"Australia","UK","USA","Canada"}.issubset({r.get("country") for r in verified}):
        failures.append("Golden set does not cover Australia, UK, USA and Canada")
    predicted={r["id"]:r.get("fields",{}) for r in observations}
    if {r["id"] for r in verified}-set(predicted):
        failures.append("Agent observations do not cover every verified golden course")
    totals,correct,flagged=Counter(),Counter(),Counter()
    for course in verified:
        for field,expected in course["fields"].items():
            if expected.get("status")!="verified":
                continue
            totals[field]+=1
            actual=predicted.get(course["id"],{}).get(field,{})
            if actual.get("status") not in {"verified","unverified","conflict"}:
                failures.append(f"Missing field outcome: {course['id']}/{field}")
            elif actual.get("status")=="verified" and actual.get("value")!=expected.get("value"):
                failures.append(f"Incorrect confidently reported field: {course['id']}/{field}")
            correct[field]+=int(actual.get("status")=="verified" and actual.get("value")==expected.get("value"))
            flagged[field]+=int(actual.get("status") in {"unverified","conflict"})
    accuracy={field:{"checked":n,"correct":correct[field],"flagged":flagged[field],
                     "accuracyPercent":round(100*correct[field]/n,2)} for field,n in totals.items()}
    if not observations:
        failures.append("Golden-set agent observations have not been produced")
    if not security_passed:
        failures.append("Prompt-injection security check has not passed")
    return {"ready":not failures,"failures":failures,"missingCases":missing,"accuracy":accuracy}


def workspace_report():
    base=Path(__file__).resolve().parents[3]
    manifest=json.loads((base/"tests/fixtures/agent_v2_scenarios.json").read_text())
    manifest["requirements"].extend(json.loads((base/"tests/fixtures/agent_staff_scenarios.json").read_text())["requirements"])
    report=base.parent/"reports/scrape-agent-benchmark.json"
    data=json.loads(report.read_text()) if report.exists() else {}
    return evaluate(manifest,data.get("golden",[]),data.get("observations",[]),
                    data.get("scenarioResults",[]),data.get("securityPassed",False))


if __name__=="__main__":
    result=workspace_report()
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result["ready"] else 1)
