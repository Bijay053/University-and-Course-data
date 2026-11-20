import pytest
from app.services.assistant_online_exclusion import evidence

URL = "https://www.uws.ac.uk/study/postgraduate/postgraduate-course-search/aesthetic-medicine/"

def page(value="Fully online", extra=""):
    return ('<main><h1>Aesthetic Medicine</h1><div class="course-details">'
            '<div class="detail-item"><p class="e-title">Location</p>'
            f'<p>{value}</p></div></div>{extra}</main>')

def test_sibling_location_value_supports_online_only():
    proof = evidence(page(), URL)
    assert proof["state"] == "checked"
    assert proof["quote"] == "Location Fully online"

@pytest.mark.parametrize("value,state", [
    ("Paisley", "on_campus"), ("Fully online", "checked"),
    ("Paisley and Online", "mixed_mode"), ("Contact admissions", "unverified"),
])
def test_card_modes(value, state):
    assert evidence(page(value), URL)["state"] == state

def test_compulsory_campus_teaching_still_vetoes_exclusion():
    assert evidence(page(extra="<p>You are required to attend on-campus teaching.</p>"), URL)["state"] == "mixed_mode"

@pytest.mark.parametrize("html", [
    page().replace('class="course-details"', 'class="support"'),
    page().replace('<p>Fully online</p>', '<p hidden>Fully online</p>'),
    page().replace('<p>Fully online</p>', '<p>Fully online</p><p>Paisley</p>'),
    '<main><h1>Aesthetic Medicine</h1></main><footer><div class="course-details">'
    '<div class="detail-item"><p>Location</p><p>Fully online</p></div></div></footer>',
    page(extra='<div class="course-details"><div class="detail-item"><p>Location</p><p>Paisley</p></div></div>'),
])
def test_ambiguous_hidden_or_unowned_cards_never_authorize_exclusion(html):
    assert evidence(html, URL)["state"] != "checked"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,heading,state", [
    ("MSc Aesthetic Medicine", "Aesthetic Medicine", "checked"),
    ("MSc Ageing and Frailty (with Dementia)", "Ageing and Frailty (with Dementia)", "checked"),
    ("Aesthetic Medicine", "MSc Aesthetic Medicine", "checked"),
    ("MSc Aesthetic Medicine", "MA Aesthetic Medicine", "unverified"),
    ("MSc Aesthetic Medicine", "PhD Aesthetic Medicine", "unverified"),
    ("MSc Aesthetic Medicine", "Aesthetic Medicine (On Campus)", "unverified"),
    ("MSc Aesthetic Medicine (January 2027)", "Aesthetic Medicine", "unverified"),
    ("MSc Aesthetic Medicine", "Medicine", "unverified"),
    ("MSc Aesthetic Medicine", "Aesthetic Medicine with Placement", "unverified"),
])
async def test_qualification_prefix_with_exact_course_url(monkeypatch, stored, heading, state):
    from app.services import assistant_source_cache
    from app.services.assistant_online_exclusion import check, SCHEMA
    async def source(*args, **kwargs):
        return {"url": URL, "verified": True, "sourceType": "html",
                "text": "Location Fully online", "courseTitle": heading,
                "_onlineExclusionSchema": SCHEMA,
                "_onlineExclusion": {"state": "checked", "name": heading,
                                     "quote": "Location Fully online"}}
    monkeypatch.setattr(assistant_source_cache, "cached_source", source)
    result = await check(None, "thread", "turn", "run", {"name": stored, "url": URL}, "https://www.uws.ac.uk")
    assert result[1] == state


@pytest.mark.asyncio
async def test_qualification_prefix_cannot_hide_conflicting_official_title(monkeypatch):
    from app.services import assistant_source_cache
    from app.services.assistant_online_exclusion import check, SCHEMA
    async def source(*args, **kwargs):
        return {"url": URL, "verified": True, "sourceType": "html",
                "text": "Location Fully online", "courseTitle": "PhD Aesthetic Medicine",
                "_onlineExclusionSchema": SCHEMA,
                "_onlineExclusion": {"state": "checked", "name": "Aesthetic Medicine",
                                     "quote": "Location Fully online"}}
    monkeypatch.setattr(assistant_source_cache, "cached_source", source)
    assert await check(None, "t", "t", "r", {"name": "MSc Aesthetic Medicine", "url": URL},
                       "https://www.uws.ac.uk") == (None, "unverified")
