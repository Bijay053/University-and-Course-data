"""Extract the supplied PDF table rows from pdftotext -bbox-layout XML."""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ns={"p":"http://www.w3.org/1999/xhtml"}
root=ET.parse(sys.argv[1])
rows=[]
counts={"scenario":0,"country":0}
for page_number,page in enumerate(root.findall(".//p:page",ns),1):
    blocks=[(float(b.attrib["xMin"]),float(b.attrib["yMin"]),
             " ".join(w.text for w in b.findall(".//p:word",ns)))
            for b in page.findall(".//p:block",ns)]
    for x,y,label in blocks:
        if not (55<x<68 and 50<y<750) or label in {"Scenario","User says","Country"} or label.startswith("Nov "):
            continue
        cells=[text for _,text in sorted((xx,text) for xx,yy,text in blocks if xx>x+30 and abs(yy-y)<1)]
        if not cells:
            continue
        kind="country" if x<61 else "scenario"
        counts[kind]+=1
        rows.append({"id":("C" if kind=="country" else "S")+f"{counts[kind]:03}",
                     "kind":kind,"page":page_number,"label":label,"cells":cells})
if counts!={"scenario":76,"country":9}:
    raise SystemExit(f"Unexpected extraction count: {counts}; review PDF layout before accepting")
destination=Path(sys.argv[2])
destination.parent.mkdir(parents=True,exist_ok=True)
destination.write_text(json.dumps({"source":"University Scraping Scenarios, Nov 5 2026","requirements":rows},indent=2)+"\n")
print(json.dumps(counts))
