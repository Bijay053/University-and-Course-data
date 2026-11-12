import pytest
from app.services.scraper.course_title_identity import complete_owned_course_title
from app.services.scraper.repair_source_urls import catalogue_allow_proposal, programme_identity
from app.services.scraper.official_catalogue_repair import _sitemap_sample
from app.services.scraper.ai_repair_live import passes

BASE="https://www.example.ac.uk"
URL=BASE+"/study/courses/advanced-computer-science-msc/september-2027"
HTML="<main><div class='course-page-header-wrapper'><h1>Advanced Computer Science</h1><p>MSc</p>" \
     "<p>Location: Birmingham</p></div><p>Duration: One year</p></main>"


@pytest.mark.asyncio
async def test_owned_award_is_used_by_real_name_extractor_without_waiving_degree_guard():
    from app.services.scraper.extractors.course_name import extract
    result=await extract(HTML,URL)
    assert result[0].value=="Advanced Computer Science MSc"
    assert result[0].method=="course_name.h1_owned_award"
    assert complete_owned_course_title(HTML,URL,"Advanced Computer Science") == result[0].value
    assert complete_owned_course_title(HTML.replace("<p>MSc</p>","<h2>MSc</h2>"),
                                       URL,"Advanced Computer Science") == result[0].value
    assert complete_owned_course_title(HTML,URL,"Different course") is None
    assert complete_owned_course_title(HTML,URL,"Advanced Computer Science MSc") is None
    from app.services.scraper.extractors.course_name import _clean
    assert _clean("Chemical Engineering BEng (Hons)")=="Chemical Engineering BEng (Hons)"


@pytest.mark.asyncio
@pytest.mark.parametrize("rendered", [False, True])
async def test_generated_h1_rule_does_not_mask_owned_award_in_normal_scrapes(monkeypatch, rendered):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.schema import UniConfig
    from app.services.scraper.pipelines import single_course
    from app.services.scraper.extractors import gemini_primary, ai_fallback
    from app.services.scraper.browser_pool import pool
    from unittest.mock import AsyncMock
    cfg=UniConfig(slug="owned-title",name="Example",base_url=BASE,scrape_url=BASE)
    cfg.extraction.skip_browser_rescue=True
    cfg.extraction.skip_per_course_browser=True
    async def forbidden(*args,**kwargs):
        raise AssertionError("Static title extraction must not fetch or call AI")
    monkeypatch.setattr(single_course,"fetch_html",forbidden)
    monkeypatch.setattr(pool,"fetch_html",forbidden)
    monkeypatch.setattr(ai_fallback,"fill_missing",forbidden)
    primary=AsyncMock(return_value=({},0.0,0,0,{"skipped":True}))
    monkeypatch.setattr(gemini_primary,"extract_primary",primary)
    html=HTML.replace("</main>", "<p>International tuition fee: £25,000 per year.</p>"
        "<p>IELTS 6.5 with no component below 6.0.</p><p>Full-time on campus.</p>"
        + "<p>Detailed curriculum and international admissions information.</p>"*60+"</main>")
    if rendered:
        from app.services.scraper import per_course_browser
        monkeypatch.setattr(per_course_browser, "maybe_browser_refetch",
                            AsyncMock(return_value=({"international_fee":25000,
                                                     "fee_currency":"GBP","fee_term":"Annual",
                                                     "ielts_overall":6.5,"duration":1,"duration_term":"Year"},
                                                    [{"field_key":"international_fee","value":25000,
                                                      "confidence":0.95,"method":"browser:international_fee",
                                                      "source_url":URL,
                                                      "snippet":"International tuition fee: £25,000 per year."}],
                                                    html,True)))
        html="<html><head><title>Advanced Computer Science</title></head>" \
             "<body><h1>Advanced Computer Science</h1></body></html>"
    token=current_uni_config.set(cfg)
    try:
        result=await single_course.extract_course(URL,html=html,use_ai_fallback=False,
                                                  extraction_rules={"course_name":{"css":"h1"}})
    finally:
        current_uni_config.reset(token)
    assert result["payload"]["course_name"]=="Advanced Computer Science MSc"
    expected="course_name.rendered_owned_award_identity" if rendered else "course_name.owned_award_identity"
    assert any(e["method"]==expected for e in result["evidence"])
    from app.services.scraper.guards import should_stage_course
    eligible,reason=should_stage_course(result["payload"]["course_name"],result["payload"],source_url=URL)
    assert eligible,reason


def test_professional_doctorate_is_a_visible_award_not_a_degree_inferred_from_url():
    html=HTML.replace("Advanced Computer Science","Hearing Therapy").replace("MSc","Professional Doctorate")
    assert complete_owned_course_title(html,URL,"Hearing Therapy")=="Hearing Therapy Professional Doctorate"
    assert complete_owned_course_title(html.replace("Professional Doctorate","Professional Certificate"),
                                       URL,"Hearing Therapy") is None


@pytest.mark.parametrize("body",[
    "<main><h1>Advanced Computer Science</h1><aside><p>MSc</p></aside></main>",
    "<main><h1>Advanced Computer Science</h1><div class='related-courses'><p>MSc</p></div></main>",
    "<main><h1>Advanced Computer Science</h1><a href='/other'><p>MSc</p></a></main>",
    "<main><h1>Advanced Computer Science</h1><p>MSc</p><p>MA</p></main>",
    "<main><div class='related-courses'><h1>Advanced Computer Science</h1><p>MSc</p></div></main>",
])
def test_related_linked_or_ambiguous_awards_are_not_borrowed(body):
    assert complete_owned_course_title(body,URL,"Advanced Computer Science") is None


def test_news_and_generic_pages_do_not_gain_course_identity():
    assert complete_owned_course_title(HTML,BASE+"/news/courses/launch","Advanced Computer Science") is None
    assert complete_owned_course_title(HTML.replace("Advanced Computer Science","Our courses"),URL,"Our courses") is None


def test_intake_aliases_share_identity_but_distinct_courses_remain_distinct():
    jan=URL.replace("september","january")
    other=URL.replace("advanced-computer-science-msc","construction-management-msc")
    assert programme_identity(URL)==programme_identity(jan)
    assert programme_identity(URL)!=programme_identity(other)
    assert len(_sitemap_sample([jan,URL],{},2))==1
    assert len(_sitemap_sample([jan,URL,other],{},2))==2
    discovery={"allow_url_patterns":["^/obsolete/"],"block_url_patterns":[]}
    patch=catalogue_allow_proposal([jan,URL,other],discovery)
    assert all(passes(url,{**discovery,**patch}) for url in [jan,URL,other])
    assert not catalogue_allow_proposal([jan,URL],discovery)
    assert not passes(URL.replace("september-2027","unrelated-page"),{**discovery,**patch})


def test_partial_whitelist_does_not_hide_new_dated_course_routes():
    from app.services.scraper.official_catalogue_repair import _repair_sample
    other=URL.replace("advanced-computer-science-msc","construction-management-msc")
    undated=[programme_identity(URL),programme_identity(other)]
    urls=[*undated,URL,URL.replace("september","january"),other]
    discovery={"allow_url_patterns":[r"^https://www\.example\.ac\.uk/study/courses/[^/]+$"]}
    patch=catalogue_allow_proposal(urls,discovery)
    assert patch and all(passes(url,{**discovery,**patch}) for url in urls)
    sample=_repair_sample(urls,{},2,discovery,patch)
    assert len({programme_identity(url) for url in sample})==2
    assert all(url not in undated for url in sample)


def test_degree_sample_prefers_explicit_awards_and_retains_bounded_alternatives():
    from app.services.scraper.official_catalogue_repair import _sample_alternatives
    links=[BASE+"/study/courses/chemical-engineering-beng/september-2027",
           BASE+"/study/courses/medical-retina-professional-certificate/march-2027",
           BASE+"/study/courses/project-management-msc/september-2027",
           BASE+"/study/courses/accounting-bsc/september-2027"]
    sample=_sitemap_sample(links,{},2)
    assert len(sample)==2 and links[1] not in sample
    alternatives=_sample_alternatives(links,{},sample)
    assert len(alternatives)<=2 and not set(sample)&set(alternatives)


@pytest.mark.asyncio
async def test_catalogue_probe_replaces_unconfirmed_course_with_distinct_bounded_alternative(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.scraper import ai_repair_live as live
    from app.services.scraper.config.schema import UniConfig
    from app.services import worker_fencing
    source=BASE+"/courses-atoz"
    links=[BASE+f"/study/courses/{name}-bsc" for name in ["computing","accounting","engineering","medical-retina"]]
    body=lambda name:f"<main><h1>{name} BSc (Hons)</h1><p>Duration: 3 years full-time.</p>" \
        "<p>International tuition fee: GBP 20000 per year.</p><p>IELTS 6.5.</p></main>"
    pages={source:"<main><h1>All courses</h1>"+"".join(f'<a href="{url}">Degree BSc (Hons)</a>' for url in links)+"</main>",
           **{url:body(url.rsplit("/",1)[-1]) for url in links}}
    pages[links[3]]="<main><h1>Support and advice</h1><p>No course information.</p></main>"
    calls=[]
    async def fetch(url,*args):
        calls.append(url)
        return pages[url],None,None
    monkeypatch.setattr(live,"_fetch_official",fetch)
    monkeypatch.setattr(worker_fencing,"reserve_live_fetch",AsyncMock(side_effect=lambda p,s,t:t))
    monkeypatch.setattr(worker_fencing,"refund_live_fetch",AsyncMock())
    cfg=UniConfig(slug="sample-alternatives",name="Example",base_url=BASE,scrape_url=BASE)
    cfg.discovery.allow_url_patterns=["^/old/"]
    evidence=live.LiveRepairEvidence({"scrape_url":BASE,"effective_config":cfg,
        "catalogue_recovery":True,"requested_course_urls":[source],"repair_url_sample":[]})
    await evidence.probe()
    assert evidence.catalogue_sample==[links[1],links[0]]
    assert links[3] in calls and links[2] not in calls
    patch={"official_catalogue_fallback":True,"sitemap_url":source,**evidence.fallback["filter_patch"]}
    result=await evidence.validate({},patch,{})
    assert result["accepted"] and evidence.pages_checked==7
    assert calls.count(source)==2, "The catalogue itself must also be replayed fresh"
