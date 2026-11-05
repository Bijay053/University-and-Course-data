"""Read-only current proposal; never resolve conflicting values by row age."""
import json,html
from pathlib import Path
from collections import defaultdict,Counter
root=Path(__file__).resolve().parents[1]
data=json.loads((root/"reports/excelsia-fresh-inventory.json").read_text())
previous=json.loads((root/"reports/excelsia-current-inventory.json").read_text())
keys=list(previous["pending"][0]["fields"])+list(previous["pending"][0]["additionalFields"])
groups=defaultdict(list)
for row in data["pending"]:
    groups[(row["course_website"].rstrip("/"),row["course_name"],row["course_location"])].append(row)
safe=[];held=[];eligibility=[]
for identity,rows in groups.items():
    if len(rows)<2:continue
    rows.sort(key=lambda r:r["id"])
    differences={k:[{"id":r["id"],"value":r.get(k)} for r in rows]
        for k in keys if len({json.dumps(r.get(k),sort_keys=True) for r in rows if r.get(k) not in [None,"",[]]})>1}
    item={"course":identity[1],"url":identity[0],"location":identity[2],"candidatePrimary":rows[0]["id"],
        "otherRows":[r["id"] for r in rows[1:]],"keepNow":[r["id"] for r in rows],
        "excludeNow":[],"conflicts":differences,"versions":{str(r["id"]):r["version"] for r in rows}}
    if not differences:
        item["keepNow"]=[rows[0]["id"]];item["excludeNow"]=item["otherRows"];safe.append(item)
    elif any(k in differences for k in ("eligibility_status","international_eligible","student_market","on_campus_available")):
        eligibility.append(item)
    else:held.append(item)
plan={"pending":len(data["pending"]),"safe":safe,"held":held,"eligibility":eligibility}
(root/"reports/excelsia-remaining-proposal.json").write_text(json.dumps(plan,indent=2))
esc=lambda x:html.escape(str(x))
def table(items):
    return "<table><tr><th>Course</th><th>Candidate primary</th><th>Other rows</th><th>Keep now</th><th>Exclude after approval</th><th>Unresolved values</th></tr>"+"".join(
        f"<tr><td><a href='{esc(g['url'])}'>{esc(g['course'])}</a><br>{esc(g['location'])}</td><td>{g['candidatePrimary']}</td><td>{esc(g['otherRows'])}</td><td>{esc(g['keepNow'])}</td><td>{esc(g['excludeNow'])}</td><td><pre>{esc(json.dumps(g['conflicts'],indent=2))}</pre></td></tr>" for g in items)+"</table>"
doc="""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Excelsia remaining consolidation proposal</title><style>body{font:16px system-ui;margin:30px;color:#172133}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:10px;vertical-align:top}th{background:#eef3f8}pre{white-space:pre-wrap;font-size:12px}aside{background:#fff4da;padding:20px}</style><h1>Excelsia remaining consolidation proposal</h1>"""
doc+=f"<aside>{len(data['pending'])} pending rows. Earlier approved consolidation: 71 → 52 (19 reversible exclusions). No further consolidation applied. {len(safe)} conflict-free groups; {len(held)} other field-conflict groups; {len(eligibility)} eligibility-conflict groups.</aside>"
doc+="<p>A candidate primary is an identity anchor, not a declaration that its values are correct. Conflicting groups retain all rows and propose no exclusions until evidence resolves the differences. Different recorded locations remain separate. Original job history and evidence must survive consolidation.</p>"
doc+="<h2>Conflict-free proposed keep/exclude groups</h2>"+table(safe)
doc+="<h2>Other field conflicts — keep all for now</h2>"+table(held)
doc+="<h2>Eligibility conflicts — separate decision required</h2>"+table(eligibility)
doc+="<h2>Confirmed current actions</h2><p>Excelsia legacy assistant automatic imports paused. Existing-course field repairs remain available. Graduate Diploma of Business Administration row 56026 has no verified intake; February/July removed with audit. No provider-wide intake assumption.</p>"
doc+="<h2>UWS checkpoint</h2><p>Award + name, duration and campus-specific starts were implemented. 94 pending rows corrected; evidence/audit for 131 rows. Only one pending undergraduate Accounting row was found; row 56120 was absent. Combined release passed 347 tests and was pushed to GitHub. AWS rollout was waiting for an active scrape; do not interpret this report as confirmation of deployment.</p></html>"
(root/"reports/excelsia-remaining-proposal.html").write_text(doc)
print(json.dumps({"pending":plan["pending"],"safe":len(safe),"otherConflicts":len(held),"eligibility":[{"course":g["course"],"rows":g["keepNow"],"values":g["conflicts"].get("eligibility_status")} for g in eligibility]},indent=2))
