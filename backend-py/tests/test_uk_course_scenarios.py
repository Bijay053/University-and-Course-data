import pytest
from app.services.scraper.extractors.uk_course_facts import clean_uk_title,surrey_options,apply_authority
from app.services.scraper.extractors import fee,course_name

URL="https://www.surrey.ac.uk/undergraduate/accounting-and-finance"
def page(label="Overseas fees"):
    panels=[]
    for key,award,duration,amount in [("standard","BSc (Hons)",3,24600),("placement","BSc (Hons) with placement",4,25000),("foundation","BSc (Hons) with foundation year",4,26000)]:
        panels.append(f'<a href="#option-{key}">{award}</a><div id="option-{key}">'+
            "".join(f"<dl><dt>{k}</dt><dd>{v}</dd></dl>" for k,v in [
                ("Full time",f"{duration} years"),(label,f"<a>£{amount:,}</a>"),
                ("UK fees","£10,050"),("Campus","Stag Hill"),("Start date","September 2027")])+"</div>")
    return '<h1><span class="t-page-title">Accounting and Finance</span><span>BSc (Hons) — 2027 entry</span></h1>'+''.join(panels)+'<section id="fees"><h3>Fees for the 2027-28 academic year</h3><div id="fees-overseas"><dl><dt>Standard year</dt><dd>£24,600</dd></dl><dl><dt>Professional training year</dt><dd>£2,010</dd></dl></div></section>'

@pytest.mark.parametrize("label",["Overseas fees","International fees","Non-UK fees"])
@pytest.mark.asyncio
async def test_uk_labels_nested_prices_preserve_year_basis_currency(label):
    values=await fee.extract(page(label),URL)
    assert values[0].normalized=={"international_fee":24600,"currency":"GBP","fee_term":"Annual","fee_year":2027}
    assert fee._INTERNATIONAL_FEE_LABEL_CTX.search(label)
    assert not fee._DOMESTIC_FEE_LABEL_CTX.search(label)

@pytest.mark.parametrize("tail",[" — 2027 Entry"," degree course 2027 entry | University of Surrey"," | University of Surrey"])
def test_uk_title_suffix_scenario(tail):
    assert clean_uk_title("Accounting and Finance BSc (Hons)"+tail)=="Accounting and Finance BSc (Hons)"

def test_options_keep_their_own_prices_durations_and_start_year():
    rows=surrey_options(page(),URL)
    assert [(r["duration"],r["international_fee"]) for r in rows]==[(3,24600),(4,25000),(4,26000)]
    assert all(r["course_location"]=="Stag Hill" and r["start_date"]=="September 2027" for r in rows)
    assert rows[1]["fee_schedule"]["Professional training year"]==2010

def test_selected_option_not_first_option():
    payload={};evidence=[]
    apply_authority(page(),URL+"?surrey_option=option-foundation",payload,evidence)
    assert payload["international_fee"]==26000 and payload["duration"]==4

@pytest.mark.asyncio
async def test_split_title_includes_award():
    result=await course_name.extract(page(),URL)
    assert result[0].value=="Accounting and Finance BSc (Hons)"

@pytest.mark.asyncio
@pytest.mark.parametrize("host",["www.lsbu.ac.uk","www.uws.ac.uk","www.surrey.ac.uk"])
async def test_uk_missing_h1_award_can_come_from_matching_document_title(host):
    html="<title>Accounting and Finance BSc (Hons) degree course 2027 entry | University of Example</title><h1>Accounting and Finance</h1>"
    result=await course_name.extract(html,"https://"+host+"/study/accounting")
    assert result[0].value=="Accounting and Finance BSc (Hons)"

def test_does_not_invent_fee_year_or_annual_basis():
    without_year=page().replace("Fees for the 2027-28 academic year","Fees")
    rows=surrey_options(without_year,URL)
    assert all(r["fee_year"] is None for r in rows)
    without_basis=without_year.replace("Standard year","Standard").replace("Professional training year","Placement")
    rows=surrey_options(without_basis,URL)
    assert all(r["fee_term"] is None for r in rows)
    payload={"international_fee":10050,"fee_term":"Annual","fee_year":2027}
    apply_authority(without_basis,URL,payload,[])
    assert payload["international_fee"]==24600 and payload["fee_term"] is None
    year_only=page().replace("Standard year","Programme total").replace("Professional training year","Placement")
    assert all(o["fee_term"] is None for o in surrey_options(year_only,URL))

@pytest.mark.asyncio
async def test_option_links_are_distinct_and_html_is_reused():
    from app.services.scraper.extractors.uk_course_facts import expand_links
    calls=[]
    async def fetch(url):calls.append(url);return page()
    links=await expand_links([{"url":URL}],10,fetch)
    assert len(links)==3 and len({x["url"] for x in links})==3 and len(calls)==1
    name=(await course_name.extract(links[2]["_surrey_html"],links[2]["url"]))[0].value
    assert "with foundation year" in name
    with pytest.raises(ValueError,match="not truncated"):
        await expand_links([{"url":URL}],2,fetch)
    from app.services.scraper.extractors.uk_course_facts import scope_option_html
    scoped=scope_option_html(page(),links[2]["url"])
    assert len(surrey_options(scoped,links[2]["url"]))==1
    assert "£25,000" not in scoped
    with pytest.raises(ValueError,match="no other option substituted"):
        scope_option_html(page(),URL+"?surrey_option=option-deleted")

def test_postgraduate_option_dates_do_not_replace_award_or_fee_year():
    html=page().replace("BSc (Hons)","MSc").replace("Fees for the 2027-28 academic year","Fees per year. These fees apply to the academic year 2027-28 only.")
    html=html.replace('>MSc</a>','>February 2027 - Full-time</a>').replace("<dt>Full time</dt>","<dt>Duration</dt>")
    rows=surrey_options(html,URL)
    assert rows[0]["course_name"]=="Accounting and Finance MSc (February 2027 - Full-time)"
    assert rows[0]["international_fee"]==24600 and rows[0]["fee_year"]==2027 and rows[0]["duration"]==3
    assert rows[0]["degree_level"]=="Master's"

@pytest.mark.asyncio
async def test_part_time_options_are_not_expanded_as_full_time():
    from app.services.scraper.extractors.uk_course_facts import expand_links
    html=page().replace("<dt>Full time</dt>","<dt>Part time</dt>")
    async def fetch(url):return html
    result=await expand_links([{"url":URL}],10,fetch)
    assert len(result)==3
    payload={}
    apply_authority(html,URL,payload,[])
    assert payload["study_load"]=="Part Time"
