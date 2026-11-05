"""Export research candidates for human approval; never grant golden status."""
import json
from pathlib import Path
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font,PatternFill,Alignment
from openpyxl.worksheet.datavalidation import DataValidation


def text(value):
    value=json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else str(value or "")
    return "'"+value if value.startswith(("=","+","-","@")) else value


def export(destination):
    wb=Workbook()
    notes=wb.active
    notes.title="Read me"
    for line in [
        "UNAPPROVED RESEARCH CANDIDATES — not the golden set",
        "No course has been approved by the agent. Team Approval starts Unreviewed.",
        "This is the existing research inventory, not a partner-selected final sample.",
        "Partner universities and recruitment-priority courses are still required.",
        "Blank cells mean missing research, not a verified absence.",
        "Original field claims and evidence are retained in the Evidence sheet.",
        "Verify every required field and its applicability before approving a course.",
        "Only team-approved, complete rows can enter the final golden set.",
        "Approval in this workbook does not automatically alter production data.",
    ]:notes.append([line])
    sheet=wb.create_sheet("Candidates")
    headers=["Candidate ID","Country","Institution","Course","Course URL","Fee","Fee basis","Currency","Fee year",
             "Intakes","Duration","IELTS / English requirements","Eligibility","Academic requirements",
             "Evidence links","Evidence snippet","Missing research fields","Team Approval","Reviewer","Review date","Team notes"]
    sheet.append(headers)
    evidence=wb.create_sheet("Evidence")
    evidence.append(["Candidate ID","Field","Research value","Research claim only","Source URL","Snippet","Header context","Basis","Currency","Year"])
    rows=[]
    for path in sorted(Path("research").glob("golden-*.json")):
        data=json.loads(path.read_text())
        rows.extend(data["courses"] if isinstance(data,dict) else data)
    def field(fields,words,exclude=()):
        return next((v for k,v in fields.items() if any(w in k.lower() for w in words)
                     and not any(w in k.lower() for w in exclude)),{})
    for row in rows:
        fields=row.get("fields",{})
        fee=field(fields,["international_fee","overseas_tuition","international_tuition"]) or field(fields,["fee","tuition"],["home","domestic","scholarship"])
        intake=field(fields,["intake","start_date"])
        duration=field(fields,["duration"])
        english=field(fields,["ielts","english"])
        eligibility=field(fields,["eligib","cricos","domestic_only","international_available"])
        academic=field(fields,["academic","entry_requirements"])
        sources=list(dict.fromkeys(v["source_url"] for v in fields.values() if v.get("source_url")))
        required={"fee":fee.get("value"),"basis":fee.get("basis"),"currency":fee.get("currency"),
                  "fee year":fee.get("year"),"intakes":intake.get("value"),"duration":duration.get("value"),
                  "English requirements":english.get("value"),"eligibility":eligibility.get("value"),"academic":academic.get("value")}
        url=row.get("url") or row.get("course_url") or ""
        required["course URL"]=url
        values=[row.get("id"),row.get("country"),row.get("institution"),row.get("name"),url,
                fee.get("value"),fee.get("basis"),fee.get("currency"),fee.get("year"),intake.get("value"),
                duration.get("value"),english.get("value"),eligibility.get("value"),academic.get("value"),
                "\n".join(sources),fee.get("snippet") or next((v.get("snippet") for v in fields.values() if v.get("snippet")),""),
                ", ".join(k for k,v in required.items() if v is None or v==""),"Unreviewed","","",""]
        sheet.append([text(v) for v in values])
        for key,value in fields.items():
            evidence.append([text(v) for v in [row.get("id"),key,value.get("value"),value.get("status"),
                value.get("source_url"),value.get("snippet"),value.get("header_evidence"),
                value.get("basis"),value.get("currency"),value.get("year")]])
    choices=DataValidation(type="list",formula1='"Unreviewed,Approved,Rejected"',allow_blank=False)
    sheet.add_data_validation(choices)
    choices.add(f"R2:R{sheet.max_row}")
    for ws in wb:
        ws.freeze_panes="A2"
        if ws!=notes:ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:
            cell.font=Font(color="FFFFFF",bold=True)
            cell.fill=PatternFill("solid",fgColor="183249")
        for cells in ws.iter_rows(min_row=2):
            for cell in cells:cell.alignment=Alignment(vertical="top",wrap_text=True)
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=32
    notes.column_dimensions["A"].width=110
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    wb.save(destination)
    checked=load_workbook(destination)
    assert checked["Candidates"].max_row-1==len(rows)
    assert all(r[0]=="Unreviewed" for r in checked["Candidates"].iter_rows(min_row=2,min_col=18,max_col=18,values_only=True))
    print(f"Exported {len(rows)} unapproved candidates and {evidence.max_row-1} field evidence entries")


if __name__=="__main__":
    export("reports/golden-candidates-team-review.xlsx")
