from app.services.scraper.extractors.uws_facts import course_details,apply_course_authority

URL="https://www.uws.ac.uk/study/undergraduate/undergraduate-course-search/accounting/"
HTML="""<h1>Accounting</h1><div class="detail-item"><p class="e-title">Award</p><p>BAcc (Hons)</p></div>
<div class="detail-item"><p class="e-title">Duration &amp; study mode</p><p>4 years full-time, typically 6 years part-time</p></div>
<div class="starting-dates"><ul><li><span>Lanarkshire:</span> September (Yrs 2 &amp; 3 only)</li>
<li><span>Paisley:</span> September</li></ul></div>"""

def test_award_duration_and_campus_restrictions():
    result=course_details(HTML,URL)
    assert result["course_name"]=="BAcc (Hons) Accounting"
    assert result["duration"]==4 and result["duration_term"]=="Year"
    assert result["campus_intakes"][0]["restriction"]=="(Yrs 2 & 3 only)"

def test_exact_campus_and_no_award_doubling():
    payload={"course_location":"Paisley"};evidence=[]
    apply_course_authority(HTML,URL,payload,evidence)
    assert payload["intake_months"]==["September"]
    assert any("Yrs 2 & 3 only" in w for w in payload["scrape_warnings"])
    assert course_details(HTML.replace("<h1>Accounting","<h1>BAcc (Hons) Accounting"),URL)["course_name"]=="BAcc (Hons) Accounting"

def test_other_campus_not_borrowed():
    payload={"course_location":"London"}
    apply_course_authority(HTML,URL,payload,[])
    assert not payload.get("intake_months")
    assert course_details(HTML,"https://example.org/course")=={}

def test_comma_and_multiple_award_durations():
    assert course_details(HTML.replace("4 years full-time, typically 6 years part-time","4 years full-time, 5 years full-time (sandwich)"),URL)["duration"]==4
    assert course_details(HTML.replace("4 years full-time, typically 6 years part-time","1 year, full-time"),URL)["duration"]==1
    assert "duration" not in course_details(HTML.replace("4 years full-time, typically 6 years part-time","5 years full-time MEng / 4 years full-time BEng"),URL)
    payload={"course_location":"Lanarkshire, Paisley"}
    apply_course_authority(HTML,URL,payload,[])
    assert payload["intake_months"]==["September"]

def test_multiple_awards_not_merged_into_single_record():
    payload={"course_name":"Accounting"}
    apply_course_authority(HTML.replace("BAcc (Hons)","MEng (Hons) / BEng (Hons)"),URL,payload,[])
    assert payload["course_name"]=="Accounting"
    assert "duration" not in payload
    assert any("requires selection" in w for w in payload["scrape_warnings"])
