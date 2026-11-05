"""Last-mile shape validation; valid shape is not proof of factual accuracy."""
import math
import re

def invalid_location(value):
    if value is None or not str(value).strip(): return False
    s=str(value).strip()
    return bool(len(s)>180 or len(s.split())>18 or not re.search(r"[^\W\d_]",s,re.UNICODE) or
        (len(s.split())>=5 and re.search(r"\b(?:is|are|will|must|should|has|have)\b",s,re.I)) or
        re.search(r"https?://|/stream|[£$€]|\b(?:tuition|fees?|amount|subject to|payable|payment|refund|scholarship|during the|please|students must|you will|is available|terms and conditions)\b",s,re.I)
        or s.casefold() in {"on campus","on-campus","online","campus","location","during the","and","or",
                           "apply now","see fees","learn more","find out more","open days","enquire now"}
        or re.search(r"[<>={}]",s))

def numeric(value):
    try:return not isinstance(value,bool) and math.isfinite(float(value))
    except (TypeError,ValueError,OverflowError):return False

def validate_fields(payload,evidence=None):
    cleaned=dict(payload);invalid={}
    if invalid_location(cleaned.get("course_location")):
        invalid["course_location"]="not a campus/city name"
    if invalid_location(cleaned.get("location_text")):
        invalid["location_text"]="not a campus/city name"
    duration=cleaned.get("duration")
    if duration is not None and (not numeric(duration) or float(duration)<=0 or
        not re.fullmatch(r"(?:year|month|week|day|hour|semester|trimester|term)s?",str(cleaned.get("duration_term") or ""),re.I)):
        invalid["duration"]="requires a positive number and a time unit"
        invalid["duration_term"]="duration could not be validated"
    fee=cleaned.get("international_fee")
    currency=cleaned.get("currency")
    if currency is not None and not re.fullmatch(r"[A-Z]{3}",str(currency)):
        invalid["currency"]="requires a currency code"
    if fee is not None and (not numeric(fee) or float(fee)<0 or
        not re.fullmatch(r"[A-Z]{3}",str(cleaned.get("currency") or ""))):
        invalid["international_fee"]="requires a number and currency code"
    for field,limit in [("ielts_overall",9),("ielts_listening",9),("ielts_reading",9),
                        ("ielts_writing",9),("ielts_speaking",9),("pte_overall",90),("toefl_overall",120)]:
        value=cleaned.get(field)
        minimum=4 if field.startswith("ielts_") else 0
        if value is not None and (not numeric(value) or not minimum<=float(value)<=limit):
            invalid[field]="score outside the supported test range"
    if invalid:
        warnings=list(cleaned.get("scrape_warnings") or [])
        for field,reason in invalid.items():
            cleaned[field]=None
            warning=f"{field} unverified: {reason}."
            if warning not in warnings:warnings.append(warning)
        if "course_location" in invalid:cleaned["location_text"]=None
        cleaned["scrape_warnings"]=warnings
        for item in evidence or []:
            if item.get("field_key") in invalid:
                item["selected"]=False;item["validation_status"]="unverified"
    return cleaned,list(invalid)
