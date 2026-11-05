"""UK title normalization and Surrey option-owned facts (no page-wide fee fallback)."""
import re
from urllib.parse import urlsplit,parse_qs
from bs4 import BeautifulSoup
from urllib.parse import urlencode,parse_qsl,urlunsplit

def option_url(url,key):
    p=urlsplit(url)
    query=[(k,v) for k,v in parse_qsl(p.query) if k!="surrey_option"]
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode(query+[("surrey_option",key)]),""))

def scope_option_html(html,url):
    if urlsplit(url).hostname not in {"www.surrey.ac.uk","surrey.ac.uk"}:return html
    selected=parse_qs(urlsplit(url).query).get("surrey_option",[None])[0]
    if not selected:return html
    soup=BeautifulSoup(html,"html.parser")
    if not soup.find(id=selected):raise ValueError("Requested Surrey option is no longer present; no other option substituted.")
    for panel in soup.select('[id^="option-"]'):
        if panel.get("id")!=selected:panel.decompose()
    for tab in soup.select('a[href^="#option-"]'):
        if tab.get("href")!="#"+selected:tab.decompose()
    return str(soup)

async def expand_links(links,max_courses,fetch=None):
    """Expand course-owned options before extraction; reuse the exact source HTML."""
    import asyncio
    if fetch is None:
        from app.services.scrape_assistant_tools import public_request
        fetch=public_request
    cache={};result=[]
    for link in links:
        url=link.get("url","")
        p=urlsplit(url)
        if p.hostname not in {"www.surrey.ac.uk","surrey.ac.uk"} or "surrey_option" in parse_qs(p.query):
            result.append(link);continue
        if url not in cache:
            raw=await asyncio.wait_for(fetch(url),15)
            cache[url]=raw.decode("utf-8",errors="replace") if isinstance(raw,bytes) else raw
        html=cache[url];options=surrey_options(html,url)
        options=[o for o in options if not o.get("part_time_only")]
        if options:
            result.extend({**link,"name":o["course_name"],"url":option_url(url,o["key"]),
                           "_surrey_html":html} for o in options)
        else:result.append({**link,"_surrey_html":html})
        if len(result)>max_courses:
            raise ValueError("Surrey option count exceeds the configured course limit; increase it before retrying. Catalogue was not truncated.")
    return result

def clean_uk_title(name):
    name=re.sub(r"\s*\|\s*.*(?:University|College).*$","",name,flags=re.I)
    name=re.sub(r"\s*(?:[—–-]\s*)?(?:20\d{2}\s+entry|entry\s+20\d{2})\s*$","",name,flags=re.I)
    name=re.sub(r"\s+degree\s+course\s*$","",name,flags=re.I)
    return re.sub(r"\s+"," ",name).strip(" —–-|")

def surrey_options(html,url):
    if (urlsplit(url).hostname or "").lower() not in {"surrey.ac.uk","www.surrey.ac.uk"}:return []
    soup=BeautifulSoup(html,"html.parser")
    title=soup.select_one("h1 .t-page-title")
    if title is None:return []
    fees=soup.select_one("#fees")
    fee_year=re.search(r"(?:Fees for the |academic year\s+)(20\d{2})",fees.get_text(" ",strip=True) if fees else "",re.I)
    annual=bool(fees and re.search(r"\b(?:standard year|per year|annual)\b",fees.get_text(" ",strip=True),re.I))
    overseas=soup.select_one("#fees-overseas")
    schedule={}
    if overseas:
        for dl in overseas.select("dl"):
            dt,dd=dl.find("dt"),dl.find("dd")
            if dt and dd:
                amount=re.search(r"£\s*([\d,]+(?:\.\d+)?)",dd.get_text(" ",strip=True))
                if amount:schedule[dt.get_text(" ",strip=True)]=float(amount[1].replace(",",""))
    options=[];seen=set()
    for tab in soup.select('a[href^="#option-"]'):
        key=tab.get("href","")[1:]
        if key in seen:continue
        seen.add(key);panel=soup.find(id=key)
        if panel is None:continue
        facts={}
        for dl in panel.select("dl"):
            dt,dd=dl.find("dt"),dl.find("dd")
            if dt and dd:facts[dt.get_text(" ",strip=True).casefold().replace("full-time","full time").replace("part-time","part time")]=dd.get_text(" ",strip=True)
        award=tab.get_text(" ",strip=True)
        name=clean_uk_title(title.get_text(" ",strip=True)+" "+award)
        if not re.search(r"\b(?:BSc|MSc|MSci|BEng|MEng|BAcc|BA|MA|LLB|LLM|MBA|MBBS|BMBS|PhD|MPhil|MRes|PGDip|PGCert)\b",award,re.I):
            name=clean_uk_title(title.find_parent("h1").get_text(" ",strip=True))+" ("+award+")"
        row={"key":key,"course_name":name,
             "source_url":url.split("#")[0]+"#"+key,"source_text":panel.get_text(" ",strip=True),
             "fee_schedule":schedule.copy()}
        from app.services.scraper.extractors.degree_level import classify_degree_level
        level,_,_=classify_degree_level(name)
        if level:row["degree_level"]=level
        row["part_time_only"]=("part time" in facts and "full time" not in facts) or bool(re.match(r"part[- ]time\b",facts.get("attendance",""),re.I))
        if row["part_time_only"]:row["study_load"]="Part Time"
        elif "full time" in facts or re.match(r"full[- ]time\b",facts.get("attendance",""),re.I):row["study_load"]="Full Time"
        if fees:
            heading=fees.find(["h3","h4"])
            row["fee_source_text"]=(heading.get_text(" ",strip=True) if heading else "")+" "+(overseas.get_text(" ",strip=True) if overseas else "")
        fee=next((v for k,v in facts.items() if re.fullmatch(r"(?:overseas|international|non[- ]uk) fees",k)),None)
        amount=re.fullmatch(r"£\s*([\d,]+(?:\.\d+)?)\s*\*?",fee or "")
        if amount:
            row.update(international_fee=float(amount[1].replace(",","")),currency="GBP",fee_term="Annual" if annual else None,fee_year=int(fee_year[1]) if fee_year else None)
            if "foundation" in award.casefold() and "Foundation year" in schedule:
                row["international_fee"]=schedule["Foundation year"]
        duration_text=facts.get("full time") or facts.get("duration","")
        row["duration_source"]=duration_text
        duration=re.fullmatch(r"(\d+(?:\.\d+)?)\s+years?",duration_text,re.I)
        if duration:row.update(duration=float(duration[1]),duration_term="Year")
        if facts.get("campus"):row["course_location"]=facts["campus"]
        start=re.fullmatch(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})",facts.get("start date",""),re.I)
        if start:row.update(intake_months=[start[1].title()],intake_year=int(start[2]),start_date=start[0])
        if facts.get("ucas code"):row["course_code"]=facts["ucas code"]
        options.append(row)
    return options

def apply_authority(html,url,payload,evidence):
    options=surrey_options(html,url)
    if not options:return
    selected=parse_qs(urlsplit(url).query).get("surrey_option",[None])[0]
    option=next((o for o in options if o["key"]==selected),None) if selected else options[0]
    if option is None:return
    if "duration" not in option:
        payload["duration"]=None;payload["duration_term"]=None
        payload["scrape_warnings"]=list(dict.fromkeys([*(payload.get("scrape_warnings") or []),
            "Duration unverified for this option: "+option.get("duration_source","not published")]))
    if "international_fee" not in option:
        for field in ("international_fee","fee_term","fee_year"):
            payload[field]=None
            for old in evidence:
                if old.get("field_key")==field:old["selected"]=False
        payload["scrape_warnings"]=list(dict.fromkeys([*(payload.get("scrape_warnings") or []),
            "International fee unverified for this Surrey option; domestic or other-option prices were not substituted."]))
    elif option.get("fee_year") is None:
        payload["scrape_warnings"]=list(dict.fromkeys([*(payload.get("scrape_warnings") or []),
            "Fee year unverified: the annual price is tied to the published option/start date, not an inferred academic year."]))
    if option.get("international_fee") is not None and option.get("fee_term") is None:
        payload["scrape_warnings"]=list(dict.fromkeys([*(payload.get("scrape_warnings") or []),
            "Fee basis unverified: the overseas amount is published, but no annual billing basis was confirmed."]))
    fields=("course_name","degree_level","international_fee","currency","fee_term","fee_year","duration","duration_term","course_location","intake_months","study_load")
    for field in fields:
        if field not in option:continue
        payload[field]=option[field]
        for old in evidence:
            if old.get("field_key")==field:old["selected"]=False
        evidence.append({"field_key":field,"value":option[field],"source_url":option["source_url"],
                         "snippet":option["source_text"]+" "+option.get("fee_source_text",""),"method":"surrey.option_owned","confidence":1.0,"selected":True})
    metadata=dict(payload.get("extraction_method") or {})
    metadata["surrey_course_options"]=[option]
    metadata["surrey_option_count"]=len(options)
    payload["extraction_method"]=metadata
    if option.get("start_date"):
        payload["scrape_warnings"]=list(dict.fromkeys([*(payload.get("scrape_warnings") or []),
            "Published start date: "+option["start_date"]]))
