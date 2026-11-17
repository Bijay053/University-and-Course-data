from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app.services.english_course_cohorts import metadata, enrich
from app.services.english_html_tables import SCHEMA, profiles, candidate
from app.services.english_applicability import applicability
from app.services.assistant_document_review import validate_changes

HOME = "https://uni.example"
HEADER = "<tr><th>School / Programme</th><th>Reading</th><th>Writing</th><th>Listening</th><th>Speaking</th><th>Overall</th></tr>"


def table(label="Engineering and Physical Sciences", level="Undergraduate", cells=None):
    cells = cells or "<td>5.5</td>" * 4 + "<td>6.0</td>"
    return f"<dl><dt>IELTS ({level})</dt><dd><table>{HEADER}<tr><th>{label}</th>{cells}</tr></table></dd></dl>"


def source(html):
    return {"verified": True, "url": HOME + "/english", "sourceType": "html",
            "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
            "_englishTablesSchema": SCHEMA, "_englishTables": profiles(html)}


def course(name="Aerospace Engineering BEng (Hons)", membership=True):
    return {"verified": True, "courseTitle": name, "_fullText": name,
            "url": HOME + "/course", "facts": [],
            "cohortEvidence": [{"label": "Engineering and Physical Sciences", "level": "undergraduate",
                                "url": HOME + "/courses?theme=10",
                                "quote": name + " Undergraduate degrees"}] if membership else []}


def row(name="Aerospace Engineering BEng (Hons)", degree="Bachelor"):
    return SimpleNamespace(id=1, course_name=name, degree_level=degree)


@pytest.mark.parametrize("name,degree", [
    ("Aerospace Engineering BEng (Hons)", "Bachelor"),
    ("Biomedical Engineering BEng (Hons)", "Bachelor"),
    ("Biomedical Engineering MEng (Hons)", "Master"),
])
def test_confirmed_catalogue_cohort_binds_scores_and_overrides_wrong_stored_level(name, degree):
    guidance, page, record = source(table()), course(name), row(name, degree)
    result = candidate(record, guidance, page)
    assert result
    assert len(validate_changes(result, guidance, page)) == 5
    proof = applicability(record, result, guidance, page)
    assert proof["applicability"] == "verified"
    assert any("theme=10" in e["url"] for e in proof["evidence"])


def test_subject_sounding_like_school_is_not_membership_proof():
    assert candidate(row(), source(table()), course(membership=False)) is None


def test_named_psychology_programmes_override_the_broader_business_school_rule():
    conditional = "<td colspan='4'>Two bands at 5.5 and other bands at 6.0 and above</td><td>6.5</td>"
    guidance = source(table("Aston Business School", cells=conditional) + table(
        "Health and Life Sciences: Biomedicine, Neuroscience, Biochemistry, Psychology all programmes",
        cells="<td>6.0</td>" * 4 + "<td>6.5</td>"))
    page = course("Psychology and Business BSc (Hons)")
    page["cohortEvidence"][0]["label"] = "Aston Business School"
    result = candidate(row(page["courseTitle"]), guidance, page)
    assert result
    assert {c["field"]: c["newValue"] for c in result["changes"]} == {
        "ielts_overall": 6.5, "ielts_listening": 6.0, "ielts_reading": 6.0,
        "ielts_writing": 6.0, "ielts_speaking": 6.0,
    }
    assert applicability(row(page["courseTitle"]), result, guidance, page)["applicability"] == "verified"
    broader = next(p for p in guidance["_englishTables"] if p["label"] == "Aston Business School")
    model_choice = {**result, "applicability": {**result["applicability"],
                    "tableId": broader["id"], "guidanceQuote": broader["quote"]},
                    "changes": [{"field": f, "newValue": v, "quote": broader["quote"], "page": None}
                                for f, v in broader["scores"].items()]}
    assert applicability(row(page["courseTitle"]), model_choice, guidance, page)["applicability"] != "verified"


def test_equally_specific_conflicting_subject_profiles_still_require_review():
    guidance = source(table("Health and Life Sciences: Psychology all programmes") +
                      table("Joint programmes: Business all programmes",
                            cells="<td>6.0</td>" * 4 + "<td>6.5</td>"))
    page = course("Psychology and Business BSc (Hons)")
    assert candidate(row(page["courseTitle"]), guidance, page) is None


def test_repeated_award_in_official_heading_is_not_a_different_mba():
    guidance = source(table("Aston Business School", level="Postgraduate"))
    page = course("MBA (Full-time) MBA")
    page["cohortEvidence"][0].update(label="Aston Business School", level="postgraduate_taught")
    assert candidate(row("MBA (Full-time)", "Master's"), guidance, page)


def test_missing_fact_components_are_not_conflicting_scores():
    page = course()
    page["facts"] = [{"field": "ielts_overall", "normalized": {
        "ielts_overall": 6.0, "ielts_listening": None, "ielts_reading": "",
    }}]
    assert candidate(row(), source(table()), page)
    page["facts"][0]["normalized"]["ielts_listening"] = 7.0
    assert candidate(row(), source(table()), page) is None


def test_canonical_selected_option_proves_a_dated_alias_without_guessing_paths():
    url = HOME + "/courses/history/september-2027"
    html = """<link rel="canonical" href="/courses/history">
      <select class="other_variant_switch"><option selected value="/courses/history">September 2027</option></select>"""
    assert metadata(html, url)["variants"][0]["url"] == HOME + "/courses/history"
    assert metadata(html.replace('href="/courses/history"', 'href="/courses/unrelated"'), url)["variants"] == []


def test_department_cards_exclude_navigation_breadcrumbs_and_unrelated_links():
    html = """<nav><a href="/other"><h3>School of Other</h3></a></nav>
      <ol class="breadcrumb"><li><a href="/college">College of Everything</a></li></ol>
      <h1>School of Law</h1><a href="/school/department"><h4>Department of Politics</h4><p>Our teaching.</p></a>
      <a href="/staff"><h3>Staff</h3></a>"""
    assert metadata(html, HOME + "/school")["academicUnits"] == [
        {"url": HOME + "/school/department", "label": "Department of Politics", "quote": "Department of Politics"}]


def test_generic_postgraduate_list_does_not_imply_taught_instead_of_research():
    html = '<h1>Department of English</h1><h2>Postgraduate</h2><a href="/courses/english">English</a>'
    assert metadata(html, HOME + "/school/department")["linkedCourses"][0]["level"] is None
    assert metadata(html.replace("Postgraduate</h2>", "Postgraduate taught</h2>"),
                    HOME + "/school/department")["linkedCourses"][0]["level"] == "postgraduate_taught"


def test_official_research_award_cannot_be_reclassified_as_taught_by_catalogue():
    page = course("Engineering MRes")
    page["cohortEvidence"][0]["level"] = "postgraduate_taught"
    assert candidate(row("Engineering MRes", "Master"), source(table(level="Postgraduate")), page) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("department_path,matched", [("/school/department", True), ("/peer-school", False)])
async def test_verified_department_chain_matches_single_subject_course_but_not_peer_school(department_path, matched):
    from unittest.mock import AsyncMock
    from app.services.scrape_assistant_tools import official_url
    school = "School of Law and Social Sciences"
    guidance = source(table(school))
    course_url = HOME + "/courses/history/september-2027"
    html = """<h1>History BA (Hons)</h1><nav><a href="/school">School of Law and Social Sciences</a></nav>
        <link rel="canonical" href="/courses/history"><select class="other_variant_switch">
        <option selected value="/courses/history">September 2027</option></select>"""
    page = course("History BA (Hons)", membership=False)
    page.update(url=course_url, _fullText=BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                _courseCohorts=metadata(html, course_url))
    root = f"""<h1>{school}</h1><a href="/school/unavailable"><h3>Department of Unavailable</h3></a>
      <a href="{department_path}"><h3>Department of Politics</h3></a>"""
    department = """<h1>Department of Politics</h1><h2>Undergraduate</h2>
      <a href="/courses/history"><h3>History</h3></a>"""
    def parsed(body, url):
        return {"verified": True, "url": url,
                "_fullText": BeautifulSoup(body, "html.parser").get_text(" ", strip=True),
                "_courseCohorts": metadata(body, url)}
    sources = {
        HOME + "/school": parsed(root, HOME + "/school"),
        HOME + department_path: parsed(department, HOME + department_path),
        HOME + "/school/unavailable": {"verified": False, "failureStage": "robots", "httpStatus": 403},
        HOME + "/courses/history": parsed(html, HOME + "/courses/history"),
    }
    reader = AsyncMock(side_effect=lambda url, home: sources[url])
    enriched = await enrich(page, guidance, HOME, reader, official_url, {})
    result = candidate(row("History BA (Hons)"), guidance, enriched)
    assert bool(result) is matched
    if matched:
        proof = applicability(row("History BA (Hons)"), result, guidance, enriched)
        assert proof["applicability"] == "verified"
        assert any(e["quote"] == "Undergraduate" for e in proof["evidence"])
        assert HOME + "/courses/history" not in [call.args[0] for call in reader.await_args_list]


@pytest.mark.asyncio
async def test_school_link_to_university_wide_level_filter_is_not_school_membership():
    from unittest.mock import AsyncMock
    from app.services.scrape_assistant_tools import official_url
    school = "School of Law and Social Sciences"
    root_url, catalogue_url = HOME + "/school", HOME + "/courses?level%5B1%5D=1"
    root_html = f'<h1>{school}</h1><a href="{catalogue_url}"><h2>Undergraduate</h2></a>'
    all_courses_html = """<h1>Courses</h1><form method="get" action="/courses">
      <label for="ug">Undergraduate</label><input id="ug" type="checkbox" name="level[1]" value="1" checked>
      </form><a href="/course"><h3 class="course-title">Aerospace Engineering BEng (Hons)</h3>
      <h5 class="program-type">Undergraduate degrees</h5></a>"""
    def parsed(html, url):
        return {"verified": True, "url": url, "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                "_courseCohorts": metadata(html, url)}
    sources = {root_url: parsed(root_html, root_url), catalogue_url: parsed(all_courses_html, catalogue_url)}
    page = course(membership=False)
    page["_courseCohorts"] = {"schoolLinks": [{"label": school, "url": root_url}]}
    enriched = await enrich(page, source(table(school)), HOME,
                            AsyncMock(side_effect=lambda url, home: sources[url]), official_url, {})
    assert enriched["cohortEvidence"] == []


def test_unresolved_subgroup_exception_blocks_general_cohort():
    html = table(level="Postgraduate") + table(
        "Engineering and Physical Sciences (Engineering Systems and Management programmes)",
        "Postgraduate", "<td>6.0</td>" * 4 + "<td>6.5</td>")
    page = course("Advanced Computer Science MSc")
    page["cohortEvidence"][0]["level"] = "postgraduate_taught"
    assert candidate(row(page["courseTitle"], "Master"), source(html), page) is None


@pytest.mark.parametrize("prose,count,span", [
    ("Two bands at 5.5 and other bands at 6.0 and above", 2, False),
    ("1 band at 5.5 as a minimum. Other bands at 6.0 and above", 1, True),
])
def test_conditional_bands_remain_constraints_never_four_numeric_minima(prose, count, span):
    cells = (f"<td colspan='4'>{prose}</td>" if span else
             f"<td>{prose}</td><td>{prose}</td><td>-</td><td>-</td>") + "<td>6.5</td>"
    guidance = source(table(cells=cells))
    page = course()
    result = candidate(row(), guidance, page)
    assert result and result["changes"] == [
        {"field": "ielts_overall", "newValue": 6.5,
         "quote": guidance["_englishTables"][0]["quote"], "page": None}]
    proof = applicability(row(), result, guidance, page)
    assert proof["bandConstraint"]["maximumBelowThreshold"] == count
    assert "Only the overall score is proposed" in proof["reason"]
    assert len(validate_changes(result, guidance, page)) == 1


def test_explicit_subject_list_is_not_a_biomedical_substring_default():
    guidance = source(table("Health and Life Sciences: Biomedical Science, Pharmacy, and all Nursing"))
    assert candidate(row("Biomedical Engineering BEng"), guidance, course("Biomedical Engineering BEng", False)) is None
    assert candidate(row("Adult Nursing BSc"), guidance, course("Adult Nursing BSc", False))


FILTER = '<form method="get" action="/courses"><label><input type="checkbox" name="theme[10]" value="10" checked>Engineering and Physical Sciences</label></form>'
CARD = '<div class="course-list-view"><a href="/course"><h3 class="course-title">Aerospace Engineering BEng (Hons)</h3><h5 class="program-type">Undergraduate degrees</h5></a></div>'


def test_catalogue_evidence_requires_both_selected_control_and_matching_query():
    html = FILTER + CARD + '<nav><a href="?theme%5B10%5D=10&page=1">Next page</a></nav>'
    parsed = metadata(html, HOME + "/courses?theme%5B10%5D=10")
    assert parsed["selected"] == ["Engineering and Physical Sciences"]
    assert parsed["records"][0]["level"] == "undergraduate"
    assert "page=1" in parsed["next"]
    assert not metadata(html, HOME + "/courses")["selected"]


def test_chrome_school_and_unfiltered_result_do_not_prove_membership():
    parsed = metadata('<footer><dl><dt>School</dt><dd>Engineering and Physical Sciences</dd></dl>'
                      + CARD + "</footer>", HOME + "/course")
    assert not parsed["schools"]
    assert not parsed["records"]


@pytest.mark.asyncio
async def test_catalogue_is_shared_and_a_partial_failure_does_not_invalidate_found_evidence():
    page = course(membership=False)
    page["_courseCohorts"] = metadata(FILTER, HOME + "/course")
    calls = []

    async def reader(url, home):
        calls.append(url)
        if "page=1" in url:
            return {"verified": False, "httpStatus": 403, "accessIssue": "robots_unavailable"}
        html = FILTER + CARD + '<a href="?theme%5B10%5D=10&page=1">Next page</a>'
        return {"verified": True, "url": url, "_courseCohorts": metadata(html, url),
                "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True)}

    cache = {}
    first = await enrich(page, source(table()), HOME, reader, lambda u, h: u, cache)
    second = await enrich(page, source(table()), HOME, reader, lambda u, h: u, cache)
    assert first["cohortEvidence"] == second["cohortEvidence"]
    assert len(calls) == 2
    assert candidate(row(), source(table()), first)


@pytest.mark.asyncio
async def test_unchecked_catalogue_filter_never_falls_back_to_all_courses():
    page = course(membership=False)
    page["_courseCohorts"] = metadata(FILTER, HOME + "/course")

    async def reader(url, home):
        html = FILTER.replace(" checked", "") + CARD
        return {"verified": True, "url": url, "_courseCohorts": metadata(html, url),
                "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True)}

    result = await enrich(page, source(table()), HOME, reader, lambda u, h: u, {})
    assert not result["cohortEvidence"]


@pytest.mark.asyncio
async def test_school_redirect_and_self_link_do_not_hide_its_course_catalogue():
    label = "School of Engineering and Physical Sciences"
    page = course(membership=False)
    page["_courseCohorts"] = {"schoolLinks": [{"label": label, "url": HOME + "/old-school"}]}
    bodies = {
        HOME + "/school": f'<h1>{label}</h1><a href="/school">Courses</a><a href="/school/courses"><h4>Courses</h4>Description</a>',
        HOME + "/school/courses": f'<h1>Courses ({label})</h1><a href="/courses?theme%5B10%5D=10"><h4>Undergraduate</h4></a>',
        HOME + "/courses?theme%5B10%5D=10": FILTER + CARD,
    }
    calls = []

    async def reader(url, home):
        calls.append(url)
        if url.endswith("/old-school"):
            return {"verified": False, "redirectUrl": HOME + "/school"}
        html = bodies[url]
        return {"verified": True, "url": url, "_courseCohorts": metadata(html, url),
                "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True)}

    enriched = await enrich(page, source(table()), HOME, reader, lambda u, h: u, {})
    assert candidate(row(), source(table()), enriched)
    assert len(calls) == len(set(calls)) == 4
    assert any(e["url"] == HOME + "/school/courses" for e in enriched["cohortEvidence"])


def test_conflicting_official_catalogue_levels_fail_closed():
    page = course()
    page["cohortEvidence"].append({**page["cohortEvidence"][0], "level": "postgraduate_taught"})
    assert candidate(row(), source(table()), page) is None


def test_intake_suffix_does_not_hide_an_otherwise_verified_programme_match():
    record = row("Aerospace Engineering BEng (Hons) (September 2027)")
    assert candidate(record, source(table()), course())
    # Qualification differences are not intake aliases.
    assert candidate(row("Aerospace Engineering MEng (Hons) (September 2027)"),
                     source(table()), course()) is None


def test_only_official_current_programme_intake_selectors_supply_variant_aliases():
    html = ('<select class="other_variant_switch"><option value="/course">September 2027</option>'
            '<option value="/course/january">January 2027</option></select>')
    assert len(metadata(html, HOME + "/course")["variants"]) == 2
    assert not metadata(html, HOME + "/another-course")["variants"]
    assert not metadata(html.replace("January 2027", "A different degree"), HOME + "/course")["variants"]


@pytest.mark.asyncio
async def test_published_intake_selector_can_bind_another_intake_to_the_same_school_catalogue():
    page = course(membership=False)
    page["url"] = HOME + "/course/september"
    page["_courseCohorts"] = metadata(
        FILTER + '<select class="other_variant_switch"><option value="/course/september">September 2027</option>'
        '<option value="/course">January 2027</option></select>', page["url"])

    async def reader(url, home):
        return {"verified": True, "url": url, "_courseCohorts": metadata(FILTER + CARD, url),
                "_fullText": BeautifulSoup(FILTER + CARD, "html.parser").get_text(" ", strip=True)}

    result = await enrich(page, source(table()), HOME, reader, lambda u, h: u, {})
    assert candidate(row(), source(table()), result)
    assert any(e["url"] == page["url"] and "January 2027" in e["quote"] for e in result["cohortEvidence"])


def test_year_one_and_two_guidance_does_not_cover_final_year_topups():
    guidance = source(table())
    guidance["_fullText"] += " The above is applicable to students entering Years 1 and 2. Final year will vary depending on the degree."
    page = course("Engineering (Top-up) BEng")
    assert candidate(row(page["courseTitle"]), guidance, page) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("same_programme", [True, False])
async def test_intake_alias_follows_only_verified_same_programme_redirects(same_programme):
    page = course(membership=False)
    page["url"] = HOME + "/course/september-2027"
    page["_courseCohorts"] = metadata(
        FILTER + '<select class="other_variant_switch"><option value="/course/september-2027">September 2027</option>'
        '<option value="/course">January 2027</option></select>', page["url"])

    calls = []
    async def reader(url, home):
        calls.append(url)
        if url == HOME + "/course":
            return {"verified": False, "redirectUrl": HOME + "/course/january-2027"}
        if url == HOME + "/course/january-2027":
            return {**(course() if same_programme else course("Chemical Engineering BEng")), "url": url}
        html = FILTER + CARD.replace('href="/course"', 'href="/course/january-2027"').replace("BEng (Hons)", "BEng(Hons)")
        return {"verified": True, "url": url, "_courseCohorts": metadata(html, url),
                "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True)}

    result = await enrich(page, source(table()), HOME, reader, lambda u, h: u, {})
    assert bool(candidate(row(), source(table()), result)) is same_programme
    assert HOME + "/course" in calls


@pytest.mark.asyncio
async def test_unique_full_award_identity_and_explicit_intake_selector_bind_year_stamped_catalogue_routes():
    page = course(membership=False)
    page["url"] = HOME + "/course/september-2027"
    page["_courseCohorts"] = metadata(
        FILTER + '<select class="other_variant_switch"><option value="/course/september-2027">September 2027</option>'
        '<option value="/course">January 2027</option></select>', page["url"])

    async def reader(url, home):
        assert "theme" in url, "No duplicate default-intake page retrieval is needed"
        html = FILTER + CARD.replace('href="/course"', 'href="/course/january-2027"')
        return {"verified": True, "url": url, "_courseCohorts": metadata(html, url),
                "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True)}

    result = await enrich(page, source(table()), HOME, reader, lambda u, h: u, {})
    assert candidate(row(), source(table()), result)
