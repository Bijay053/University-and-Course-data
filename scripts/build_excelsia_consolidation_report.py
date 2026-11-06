"""Generate a read-only, identity-scoped consolidation proposal; never writes DB."""
import json,html
from pathlib import Path
from collections import defaultdict

root=Path(__file__).resolve().parents[1]
data=json.loads((root/"reports/excelsia-current-inventory.json").read_text())
groups=defaultdict(list)
for row in data["pending"]:
    groups[(row["url"].rstrip("/"),row["name"],row["location"])].append(row)
safe,held=[],[]
for key,rows in groups.items():
    if len(rows)<2:
        continue
    rows.sort(key=lambda r:r["id"])
    fields=[{**r["fields"],**r["additionalFields"]} for r in rows]
    conflicts={k:[{"id":r["id"],"value":f[k]} for r,f in zip(rows,fields)]
               for k in fields[0]
               if len({json.dumps(f[k],sort_keys=True) for f in fields if f[k] not in [None,"",[]]})>1}
    entry={"name":key[1],"url":key[0],"location":key[2],"retain":rows[0]["id"],
           "duplicateIds":[r["id"] for r in rows[1:]],"conflicts":conflicts,
           "sourceJobIds":sorted({r["job"] for r in rows}),
           "rowVersions":{str(r["id"]):r["version"] for r in rows}}
    (held if conflicts else safe).append(entry)
plan={"status":"proposal_not_applied","pendingRows":len(data["pending"]),"publishedRows":len(data["published"]),
      "candidates":safe,"held":held,"proposedSoftExclusions":sum(len(g["duplicateIds"]) for g in safe)}
(root/"reports/excelsia-consolidation-proposal.json").write_text(json.dumps(plan,indent=2))
escape=lambda v:html.escape(str(v))
def table(entries):
    return "<table><tr><th>Course / location</th><th>Retain</th><th>Other row IDs</th><th>Conflicts</th></tr>"+"".join(
        f"<tr><td><a href='{escape(g['url'])}'>{escape(g['name'])}</a><br>{escape(g['location'] or 'Location unrecorded')}</td>"
        f"<td>{g['retain']}</td><td>{', '.join(map(str,g['duplicateIds']))}</td><td>"
        +("<details><summary>"+escape(", ".join(g["conflicts"]))+"</summary><pre>"+escape(json.dumps(g["conflicts"],indent=2))+"</pre></details>" if g["conflicts"] else "No conflicts in compared course fields")
        +"</td></tr>" for g in entries)+"</table>"
document=f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Excelsia consolidation proposal</title><style>body{{font:16px system-ui;max-width:1100px;margin:35px auto;padding:20px;color:#172133}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;text-align:left;border:1px solid #ddd;vertical-align:top}}th{{background:#eef3f8}}a{{color:#125da3}}pre{{white-space:pre-wrap;font-size:12px}}aside{{background:#fff4d6;padding:18px}}h2{{margin-top:35px}}</style>
<h1>Excelsia review consolidation</h1><p>Proposal only — no duplicates excluded yet. Prepared 5 November 2026.</p>
<aside>{len(data['pending'])} pending rows across {len(set(r['job'] for r in data['pending']))} source reviews; {len(data['published'])} published records.
Proposed: {plan['proposedSoftExclusions']} reversible soft exclusions across {len(safe)} groups. {len(held)} groups remain held for conflicting values.</aside>
<p>Retain the listed row, preserve complementary values and source evidence from duplicates, and retain the original duplicate records in soft-excluded state for audit and undo. Do not overwrite conflicting values. Revalidate every row version before applying.</p>
<p>The intended consolidated provider review must preserve original scrape-job history. No published record is included. Different recorded locations are kept separate; unrecorded location is not assumed to mean Sydney.</p>
<h2>Proposed groups requiring approval</h2>{table(safe)}
<h2>Held groups — do not exclude</h2>{table(held)}
<p>This is a consolidation proposal, not a verification of every course field or approval to publish. If source evidence or row versions change, reassess rather than force the plan.</p></html>"""
(root/"reports/excelsia-consolidation-proposal.html").write_text(document)
print(json.dumps({k:plan[k] for k in ("pendingRows","publishedRows","proposedSoftExclusions")}))
