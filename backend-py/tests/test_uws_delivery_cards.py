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
