"""Check saved research excerpts without promoting candidates to ground truth."""
import json
import re
from pathlib import Path

root=Path(__file__).resolve().parents[2]
sources={}
courses=[]
gaps=[]
for path in sorted((root/"research").glob("golden-*.json")):
    data=json.loads(path.read_text())
    courses.extend(data["courses"])
    gaps.extend(data.get("gaps",[]))
    for source in data["sources"]:
        sources[source["url"]]=source
normalize=lambda text:re.sub(r"\s+"," ",text).strip()
checks=[]
for course in courses:
    for field,fact in course["fields"].items():
        if fact.get("status")!="verified":
            continue
        source=sources.get(fact.get("source_url"),{})
        path=root/source.get("path","__missing__")
        snippet=normalize(fact.get("snippet") or "")
        text=normalize(path.read_text()) if path.is_file() else ""
        checks.append({"course":course["id"],"field":field,
                       "exactExcerptFound":bool(snippet and snippet in text)})
registry={f"source-{i:03}":source for i,source in enumerate(sources.values(),1)}
(root/"research/sources.json").write_text(json.dumps(registry,indent=2)+"\n")
result={"candidateCount":len(courses),"verifiedFieldClaims":len(checks),
        "exactExcerptMatches":sum(c["exactExcerptFound"] for c in checks),
        "mismatches":[c for c in checks if not c["exactExcerptFound"]],
        "approvedGoldenSet":False,
        "note":"Excerpt matching does not verify applicability, interpretation or current-year authority."}
(root/"research/candidate-evidence-check.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result,indent=2))
