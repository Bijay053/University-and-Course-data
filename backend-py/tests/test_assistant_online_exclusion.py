import pytest

from app.services.assistant_online_exclusion import evidence, intent, selection

URL = "https://www.aston.ac.uk/study/courses/hearing-therapy"
NAME = "Hearing Therapy Professional Doctorate"


def page(details="", label="Online learning"):
    return (f'<main><div class="course-page-header-wrapper"><h1>{NAME}</h1>'
            f'<p><strong>Location: {label}</strong></p></div>{details}</main>')


@pytest.mark.parametrize("prompt,execute", [
    ("reject online only courses", True),
    ("Exclude online-only courses for Aston only", True),
    ("remove online courses from pending", True),
    ("WHY THERE IS STILL ONLINE COURSES LISTED IN PENDING LIT FIX IT", False),
    ("WHY THERE IS STILL ONLINE COURSES LISTED IN PENDING list FIX IT", False),
    ("Show a preview to reject online-only courses", False),
])
def test_actual_staff_requests_prepare_a_complete_operation(prompt, execute):
    assert intent(prompt) == {"prompt": prompt, "execute": execute}


@pytest.mark.parametrize("prompt", [
    "do not reject online courses", "don't remove online courses",
    "never exclude online-only courses", "only explain online courses in pending",
    "read-only check online courses", "remove online labels from these courses",
    "reject online-only courses for all universities",
    "Why did you reject online-only courses?",
])
def test_no_authority_in_negated_explanatory_or_global_requests(prompt):
    assert intent(prompt) is None


def test_do_it_uses_last_staff_command_not_the_assistants_sampled_claim():
    messages = [
        {"role": "user", "content": "reject online only courses"},
        {"role": "assistant", "content": "Only the one sampled course is online-only."},
        {"role": "user", "content": "do it"},
    ]
    assert intent("do it", messages)["prompt"] == "reject online only courses"
    assert intent("do it", [{"role": "assistant", "content": "reject online only courses"}]) is None
    assert intent("do it", messages + [{"role": "user", "content": "Check MBA fees"}]) is None


def test_generic_command_checks_every_pending_row_not_just_sampled_or_online_labelled_rows():
    rows = [{"id": n, "name": f"Programme {n}", "url": f"https://uni.edu/course/{n}"}
            for n in range(1, 150)]
    assert len(selection("reject online only courses", rows)) == 149
    assert selection("reject online-only course Programme 10", rows) == rows[9:10]
    assert selection("reject online-only course Missing Award", rows) == []
    assert selection("reject online-only row 999", rows) == []
    assert selection("reject online-only row 10; leave others unchanged", rows) == rows[9:10]
    assert selection("reject online-only Programme 10; leave others unchanged", rows) == rows[9:10]
    assert selection("reject all online courses except row 10", rows) == []


def test_hearing_therapy_five_required_campus_days_are_not_online_only():
    proof = evidence(page("<p>This course is predominantly taught online. There are five days "
                          "in which you are required to attend live teaching (on-campus), "
                          "and dates are released in advance.</p>"), URL)
    assert proof["state"] == "mixed_mode"
    assert "required" in proof["campusQuote"]
    assert not proof.get("quote")


@pytest.mark.parametrize("details,state", [
    ("<p>All teaching is delivered online.</p>", "checked"),
    ("<p>You may attend optional on-campus workshops.</p>", "checked"),
    ("<p>You are not required to attend on-campus teaching.</p>", "checked"),
    ("<p>On-campus teaching is compulsory for all students.</p>", "mixed_mode"),
    ("<p>You must attend in-person workshops.</p>", "mixed_mode"),
    ("<p>Teaching sessions take place on-campus.</p>", "unverified"),
    ("<nav><p>You must attend on-campus teaching.</p></nav>", "checked"),
    ('<section class="related-courses"><p>You must attend on-campus classes.</p></section>', "checked"),
])
def test_mandatory_teaching_is_distinct_from_optional_visits_and_chrome(details, state):
    assert evidence(page(details), URL)["state"] == state


@pytest.mark.parametrize("label,state", [
    ("Online", "checked"), ("Birmingham", "on_campus"),
    ("Birmingham and Online", "mixed_mode"), ("Ask admissions", "unverified"),
])
def test_structured_labels_not_stored_online_flags_are_authoritative(label, state):
    assert evidence(page(label=label), URL)["state"] == state


def test_generic_provider_definition_list_preserves_literal_quote():
    proof = evidence("<main><h1>Data Science MSc</h1><dl><dt>Delivery</dt><dd>Online</dd></dl></main>",
                     "https://uni.edu/courses/data-science")
    assert proof["state"] == "checked" and proof["quote"] == "Delivery Online"


def test_primary_online_label_does_not_override_an_international_campus_panel():
    html = page('<section data-audience="international"><p>Location: Birmingham</p></section>')
    assert evidence(html, URL)["state"] == "unverified"


@pytest.mark.parametrize("html,url", [
    ("<main><h1>Course</h1><p>Online student resources and support</p></main>", URL),
    (page(), URL + "?course-option=campus"),
    (page(), "https://www.aston.ac.uk/news/hearing-therapy"),
    ('<main><h1>Course</h1><p hidden>Delivery: Online</p></main>', URL),
])
def test_traps_and_unverified_selected_options_cannot_support_rejection(html, url):
    assert evidence(html, url)["state"] == "unverified"


@pytest.mark.asyncio
async def test_old_flattened_or_wrong_course_sources_cannot_be_used(monkeypatch):
    from app.services import assistant_source_cache
    from app.services.assistant_online_exclusion import check
    async def source(*args, **kwargs):
        assert kwargs["online_exclusion_schema"] == 1
        return {"verified": True, "sourceType": "html", "_onlineExclusion": {
            "state": "checked", "name": "Different Award", "quote": "Delivery: Online"}}
    monkeypatch.setattr(assistant_source_cache, "cached_source", source)
    assert await check(None, "thread", "turn", "run", {"url": URL, "name": NAME},
                       "https://www.aston.ac.uk") == (None, "unverified")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,outcome", [
    ("Data Science MSc (January 2027)", "checked"),
    ("Data Science MSc (April 2027)", "unverified"),
    ("Data Science MSc (Online)", "unverified"),
])
async def test_selected_dated_title_alias_never_erases_a_different_intake_or_mode(monkeypatch, name, outcome):
    from app.services import assistant_source_cache
    from app.services.assistant_online_exclusion import check
    url = "https://www.aston.ac.uk/study/courses/data-science/january-2027"
    async def source(*args, **kwargs):
        return {"url": url, "verified": True, "sourceType": "html", "text": "Delivery: Online",
                "_onlineExclusionSchema": 1, "_onlineExclusion": {
                    "state": "checked", "name": "Data Science MSc", "quote": "Delivery: Online"}}
    monkeypatch.setattr(assistant_source_cache, "cached_source", source)
    result = await check(None, "thread", "turn", "run", {"url": url, "name": name},
                         "https://www.aston.ac.uk")
    assert result[1] == outcome
