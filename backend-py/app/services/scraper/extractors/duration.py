"""Course-duration extractor.

Ported from Node ``extractDurationFromTextBlock`` /
``extractDurationFromDom`` in ``artifacts/api-server/src/routes/scrape.ts``
(lines 3453-3556).

Returns one ExtractionResult with the course duration plus its term
(Year / Semester / Trimester / Month / Week). Excludes accelerated /
fast-track variants — they should not overwrite the standard duration
(real-world bug at CSU "Bachelor of Business Studies").
"""
from __future__ import annotations

import re

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult


field_key = "duration"

_LABELS = (
    r"course\s*duration|duration|course\s*length|program\s*length|"
    r"study\s*duration|full[- ]?time\s*duration"
)
_UNIT = r"years?|yrs?|months?|weeks?|trimesters?|semesters?"


def _from_aut_points(html: str, url: str) -> tuple[float, str] | None:
    """Derive AUT full-time duration from its canonical programme points.

    AUT defines 120 points as one full-time year. Some pages carry stale or
    compressed ``meta[name=duration]`` values (for example, 180 points paired
    with "1 year"), so the points metadata is the consistent workload source.
    """
    if not re.search(r"https?://(?:www\.)?aut\.ac\.nz/", url, re.I):
        return None
    match = re.search(
        r"<meta\b[^>]*\bname=[\"']points[\"'][^>]*\bcontent=[\"']"
        r"(\d+(?:\.\d+)?)[\"'][^>]*>",
        html,
        re.I,
    )
    if match is None:
        # Attribute order is not guaranteed.
        match = re.search(
            r"<meta\b[^>]*\bcontent=[\"'](\d+(?:\.\d+)?)[\"'][^>]*"
            r"\bname=[\"']points[\"'][^>]*>",
            html,
            re.I,
        )
    if match is None:
        return None
    points = float(match.group(1))
    years = points / 120.0
    if not 0.1 <= years <= 10:
        return None
    return years, f'AUT meta[name="points"]: {points:g} points / 120 points per year'

_PATTERNS = (
    re.compile(rf"\b(?:{_LABELS})\b[\s:.\-]{{0,40}}(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I),
    re.compile(rf"\bfull[- ]?time\b[\s:.\-]{{0,20}}(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I),
    re.compile(rf"\b(\d+(?:\.\d+)?)\s*({_UNIT})\s*(?:full[- ]?time)?\b", re.I),
    # JSON / data-attribute shape used by NextJS-driven CMSs (Federation
    # University and similar): the static HTML embeds key/value blocks
    # like  "heading": "Duration", "summary": "3 years full-time".  The
    # character class in pattern 0 ([\s:.\-]) excludes commas and quotes,
    # so this wider gap regex (allowing the JSON punctuation between
    # the label and the value) is required to lift the value out of the
    # serialised JSON.  Appended at the END (index 3) so the existing
    # `pat_idx == 2` loose-fallback gate at line ~754 continues to target
    # only the bare-number fallback; this pattern carries its own label
    # context and is treated as a strong (label-priority) signal.
    re.compile(
        rf"""(?:{_LABELS})['"]?\s*[:,]\s*['"]?\s*(?:summary|value|text)?\s*['"]?\s*[:,]?\s*['"]\s*"""
        rf"""(\d+(?:\.\d+)?)\s*({_UNIT})\b""",
        re.I,
    ),
)

# Recommendation widgets repeat other courses' fact boxes on the current
# course page. Their explicit "Duration" labels look just as authoritative as
# the real course summary after HTML is flattened, and the tournament then
# prefers the largest value (e.g. UOW Master of Teaching = 2 years, while a
# "Courses you might like" Bachelor card says 4 years). Strip only containers
# with high-precision related/recommended-course markers.
_RELATED_COURSE_CONTAINER_RE = re.compile(
    r"(?:"
    r"related[-_ ]?courses?|"
    r"recommended[-_ ]?courses?|"
    r"similar[-_ ]?courses?|"
    r"courses?[-_ ]?you[-_ ]?might[-_ ]?like|"
    r"course[-_ ]?recommendations?|"
    r"course__like"
    r")",
    re.IGNORECASE,
)
_RELATED_COURSE_HEADING_RE = re.compile(
    r"^(?:"
    r"courses?\s+you\s+might\s+like|"
    r"you\s+may\s+also\s+like|"
    r"related\s+courses?|"
    r"similar\s+courses?|"
    r"recommended\s+courses?"
    r")$",
    re.IGNORECASE,
)
_ACCELERATED = re.compile(
    r"\b(accelerat(?:ed|ion)|fast[- ]?track|condensed|intensive\s+(?:mode|stream|study)|"
    r"advanced\s+standing|recognition\s+of\s+prior\s+learning|RPL|"
    r"credit\s+for\s+previous\s+study)\b",
    re.I,
)
# A page can publish the standard and accelerated durations in one value:
#   "Duration 5 years full-time ... 4 years full-time accelerated program"
# Rejecting the whole sentence loses the authoritative standard duration.
# Remove only the numeric alternative attached to the accelerated marker; if
# no separate standard value remains, the existing fail-closed behaviour wins.
_ACCELERATED_DURATION_CLAUSE_RE = re.compile(
    rf"(?:\bor\b|[,;/])?\s*"
    rf"\d+(?:\.\d+)?\s*(?:{_UNIT})\b"
    rf"(?:(?!\d+(?:\.\d+)?\s*(?:{_UNIT})\b)[^.;\n]){{0,100}}?"
    rf"\b(?:accelerat(?:ed|ion)|fast[- ]?track|condensed|"
    rf"intensive\s+(?:mode|stream|study))\b[^.;\n]*",
    re.I,
)
# Sentences that mention credit points/units in the same span as a number+year
# match are credit-point talk (e.g. "Masters: 5 units of 8 credit points each
# across 2 years"), not the actual program duration. Without this filter, the
# extractor caught `5 units` and emitted "5 Year" for postgrad courses — exact
# bug the user reported (Masters showing 5 instead of 2).
_CREDIT_POINT_CONTEXT = re.compile(
    r"\b(credit\s+points?|cp\b|subjects?\s+(?:per|of)|units?\s+(?:per|of)|"
    # VU unit-list rows render as "Unit code: NIT1001 | Credits: 12 Year 1,
    # Semester 2 ..." after html_to_text flattens the DOM.  Pattern-3
    # (`<num> <unit>`) then matches "12 Year" by gluing the per-unit credit
    # value to the next semester heading, and the bachelor floor sanity
    # check nullifies the resulting 12-Year duration entirely (real bug:
    # VU "Bachelor of Data Science", "Bachelor of Cyber Security",
    # "Bachelor of Dermal Sciences", etc., all had duration "-" in the
    # review table).  Treat any "Credits: <num>" cell as credit-point
    # context so Pattern-3 in that sentence is demoted ×0.01.
    r"credits?\s*[:=])\b",
    re.I,
)
# PR-1.5 prod regression: VIT MBA rows staged with duration=10 Year because
# the loose fallback pattern 3 (`\b<num>\s*<unit>\b`) matched "10 years" in
# unrelated copy ("over 10 years of industry partnerships",
# "celebrating 10 years", "10 years experience"). Pattern 3 now ONLY fires
# inside a sentence that also names a duration-related concept — without
# this guard ANY "<num> <unit>" anywhere on the page can win the
# weight-by-weeks tournament and clobber the real program length.
#
# Patterns 1 and 2 are already context-bound (require an explicit duration
# label or "full-time"), so they're not gated on this filter.
_DURATION_CONTEXT = re.compile(
    r"\b(course|programme?|degree|study|studies|complete|completion|"
    r"duration|length|full[- ]?time|part[- ]?time|fulltime|parttime|"
    r"qualification|enrolment|enrol|takes|lasting|over\s+\d|spread\s+over)\b",
    re.I,
)
# Negative context — even when a duration-context word appears nearby, a
# few specific phrases mean the number is NOT a program length:
#   • "experience" / "years experience" — staff bio, industry tenure.
#   • "established" / "founded" / "since 19xx" — institutional history.
#   • "partnership" / "anniversary" / "celebrating" — marketing copy.
# Match anywhere in the same sentence; if any of these fire we skip the
# pattern-3 hit entirely, even if a (course|program|...) keyword is also
# in the sentence (e.g. "celebrating 10 years of our MBA program" —
# "program" passes _DURATION_CONTEXT but "celebrating" disqualifies it).
_DURATION_ANTI_CONTEXT = re.compile(
    r"\b(experience|established|founded|since\s+(?:19|20)\d{2}|"
    r"anniversar(?:y|ies)|celebrat(?:e|ing|ion|ed)|partnership|"
    r"history|track\s+record|over\s+a\s+decade|years?\s+of\s+industry|"
    # PSYCH/HDR eligibility clauses: "no earlier than 8 years previous to
    # the year of application" — not a program duration.
    r"previous\s+to|year\s+of\s+application|year\s+of\s+enrol(?:lment)?|"
    # Recency-window eligibility clauses — e.g. "completed within the last
    # 10 years", "awarded in the last 8 years", "obtained within the past
    # 5 years", "completed in the past 15 years".  These appear on admission
    # requirements pages and produce false-positive duration values (e.g.
    # ACU "Master of Psychology (Clinical)" → 10 years, AIT ICT50220 → 15
    # years).  The number in these clauses is a recency window, not the
    # program length.  Match the preposition phrase that introduces the window
    # so the entire sentence is disqualified from Pattern-2 firing.
    r"within\s+the\s+(?:last|past)|in\s+the\s+(?:last|past)|"
    r"(?:no\s+more\s+than|not\s+more\s+than|at\s+least)\s+\d+\s+years?\s+(?:prior|before|ago)|"
    # Marketing / institutional copy: "over X years" without an explicit
    # duration label is almost always history/tenure, not a program length.
    r"over\s+\d+\s+years?\s+(?:of|in|as)|"
    # "received within", "awarded within", "granted within" — qualification
    # currency checks that reference time elapsed, not program length.
    r"received\s+within|awarded\s+within|granted\s+within|obtained\s+within|"
    # Completion-deadline clauses that appear on Graduate Certificate / Diploma
    # pages alongside the real program duration:
    #   "must complete the qualification within 8 years of commencement"
    #   "candidates must complete within 8 years of enrolment"
    #   "within 8 years of commencement of studies"
    #   "complete their qualification within 8 years"   ← no "of commencement"
    #   "complete all subjects within 8 years"
    # These are academic time limits, not program lengths.  The year value
    # in the deadline sentence scores much higher than an 8-month program
    # duration (8 Year → 41,604 vs 8 Month → 3,202) and wins the tournament
    # incorrectly.  The gate is Pattern-2-only so Pattern-0 labeled sentences
    # (e.g. "Duration: 2 years full-time. Must complete within 4 years.") are
    # unaffected — Pattern-0 priority (×100) dominates regardless.
    #
    # NOTE: The original pattern required "complete" to be immediately before
    # "within" (only whitespace between them).  KBS uses phrasing like
    # "complete their qualification within 8 years" — 1–4 intervening words —
    # which the old pattern missed.  Allow up to 4 intervening words so all
    # common phrasings are covered while keeping the pattern precise.
    r"within\s+\d+\s+years?\s+of\s+(?:commencement|commencing|enrol(?:ment|ling)?|"
    r"starting|graduation|admission|candidature|award)|"
    r"complet(?:e|ed|ing|ion)(?:\s+\w+){0,4}\s+within\s+\d+\s+years?|"
    # Admission-requirements boilerplate.  These sentences contain "completed"
    # which triggers _DURATION_CONTEXT, so Pattern-2 would otherwise fire on
    # "12 years" and beat the real program duration (e.g. Torrens grad certs
    # showing "12 years of schooling required" → 12 Year wins over 20 Month).
    #   "completed Year 12 or equivalent"
    #   "12 years of schooling"
    #   "12 years of secondary/high-school education"
    #   "equivalent to Year 12"
    r"year\s+12\b|"
    r"\d+\s+years?\s+of\s+(?:schooling|secondary|high\s?school|education)|"
    r"equivalent\s+to\s+year\s+\d+|"
    r"completed?\s+year\s+\d+|"
    # UK university course-module year-selector tabs (e.g. UEL, Coventry) render
    # as "Select year Year 1 Year 2 Year 3" in plain text after html_to_text.
    # Pattern-3 then matches "1 Year" (from "Year [1 Year] 2") and wins the
    # weight tournament with duration=1, beating the real 3-year Gemini value.
    # Two consecutive "Year N" occurrences is a reliable tab-selector signal;
    # "select year" as a phrase is a direct match of the tab heading.
    r"select\s+year\b|"
    r"year\s+\d+\s+year\s+\d+|"
    # International admissions country-equivalency tables: UK universities
    # publish per-country boilerplate like "We normally consider the
    # following qualifications for entry to our postgraduate taught
    # programmes: Bachelor Degree (minimum 5 years) or Master ...". The
    # "(minimum N years)" here describes the length of the APPLICANT'S
    # PRIOR qualification for a given country, not this course's duration —
    # but it satisfied _MINIMUM_DURATION_RE and won the tournament at
    # Pattern-0 priority (real bug: QMUL — every single course page staged
    # duration="5 Year" because every page repeats this admissions-country
    # table, regardless of the course's actual length, e.g. an 8-month
    # PGCert). Disqualify any sentence that is clearly this entry-requirement
    # equivalency boilerplate.
    r"qualifications?\s+for\s+entry|"
    r"consider\s+the\s+following\s+qualifications|"
    r"entry\s+to\s+our\b|"
    r"equivalent\s+(?:to\s+)?(?:a\s+)?(?:UK\s+)?(?:bachelor|honours|master)s?\s+degree)\b",
    re.I,
)

# Research-degree completion caps and part-time equivalents must NEVER be
# used as the program duration.  These appear on HDR (Higher Degree by
# Research) pages alongside the real "Duration: 2 years" label and have
# much larger numbers (e.g. "maximum candidature: 8 years").  Without
# this filter the loose pattern-3 fallback picks "8 years" and wins the
# weight tournament over "2 years" because "course" / "completion" words
# are nearby.
_DURATION_RESEARCH_CAP_RE = re.compile(
    r"\b(?:maximum\s+(?:candidature|completion(?:\s+time)?|time|period)|"
    r"max(?:\.|imum)?\s+(?:candidature|completion|time)|"
    r"part[- ]?time\s+equivalent|"
    r"research\s+period|"
    r"thesis\s+(?:submission|completion)|"
    r"minimum\s+(?:candidature|time)|"
    r"maximum\s+enrolment)\b",
    re.I,
)
# Extension clauses in duration value cells (e.g. "with the possibility of a
# six to 12 month extension") must never win over the stated nominal duration.
# Used in _classify_duration_value to skip digit matches embedded in these clauses.
_EXTENSION_CTX_RE = re.compile(r"\bextension\b", re.I)

# Sentences about field placement, practicum, or professional experience
# must NEVER contribute to the duration tournament — the time value in such
# sentences is practice hours, not program length.
#   e.g. "complete at least 80 days (16 weeks) of full-time placement in
#         primary education settings."
# Pattern-1 gates on "full-time" and would otherwise match "16 weeks" here.
# Without this guard those 16 weeks convert to 4 months and, for a
# bachelor-level course, the bachelor-floor sanity check (<2 years) nullifies
# the value — dropping the duration entirely even when the AI fallback
# correctly derived 4 years from the rest of the page.
_PLACEMENT_CONTEXT_RE = re.compile(
    r"\b(?:placement|practicum|prac\b|professional\s+experience|"
    r"field\s+(?:placement|experience|work)|work\s+(?:placement|experience)|"
    r"clinical\s+(?:placement|experience|practice)|"
    r"teaching\s+(?:placement|practice|rounds?)|"
    r"industry\s+placement|internship\s+(?:hours?|days?|weeks?))\b",
    re.IGNORECASE,
)
# UK universities often note an optional placement year as an extension of
# the nominal program duration:
#   "Full time: 4 Years, or 5 if a work placement is chosen"
#   "3 years full-time, or 4 with an industrial placement year"
# These clauses contain the word "placement" but describe an OPTIONAL
# extension year, not placement-hours / practicum context. Strip them
# before checking _PLACEMENT_CONTEXT_RE so Pattern-1 is not suppressed
# for the labeled full-time duration that precedes the clause.
_PLACEMENT_OPT_YEAR_RE = re.compile(
    r",?\s*or\s+\d+(?:\.\d+)?\s*(?:years?)?\s*"
    r"(?:if\s+(?:a\s+)?|with\s+(?:a(?:n)?\s+)?)"
    r"(?:(?:work|industrial|professional|sandwich|optional|integrated)\s+)?"
    r"placement\b[^,;\n]*",
    re.IGNORECASE,
)

# "Minimum 2 years, up to a maximum of 5 years" — always prefer the MINIMUM
# (floor) duration when the page advertises a range.  Without this the weight
# tournament can pick "5 years" because a duration-context word ("complete",
# "completion", "course") happens to appear closer to the maximum number.
# This fires in BOTH _classify_duration_value (structural DOM pre-pass) and
# the sentence-level tournament loop in extract().
_MINIMUM_DURATION_RE = re.compile(
    r"\b(?:minimum|min\.?|at\s+least|from)\s+(\d+(?:\.\d+)?)\s*"
    r"(years?|yrs?|months?|weeks?|semesters?|trimesters?)\b",
    re.IGNORECASE,
)

# Sentences that describe a combined / add-on / double degree listed inline
# on a course page must never win the duration tournament over the main
# program's labeled "Duration: N years" sentence.
#
# Root cause (Flinders Bachelor of Science):
#   Pattern-0 fires on BOTH:
#     "Duration: 3 years"   → weight 1,560,400  (main degree, correct)
#     "Add on Bachelor of Laws … SATAC code: 245041  Duration: 5 years"
#                           → weight 2,600,400  (add-on, WRONG)
#   The combined-degree sentence has a larger number, so it wins.
#
# Fix: demote Pattern-0 matches inside such sentences by a 0.001 multiplier
# so the main degree's Duration label always dominates.  We do NOT drop them
# entirely (demote-not-drop) so that if the main degree page only advertises a
# combined duration we still emit something.
_COMBINED_DEGREE_CONTEXT_RE = re.compile(
    r"\b(?:add[- ]on|combined\s+degree|combined\s+program(?:me)?|"
    r"double\s+degree|joint\s+degree|dual\s+degree|"
    r"conjoint|bachelor[- ]?of[- ]?laws\s+combined|"
    # SATAC codes appear in combined-degree blurbs on Flinders pages —
    # a Duration: label immediately after a SATAC code is always a
    # combined-degree duration, not the main program duration.
    # Use \b after "code" (not after ":") so the word boundary fires at
    # the transition from the word "code" to the non-word ":".
    r"satac\s+code\b|"
    # Generic: "as part of the combined/double program"
    r"as\s+part\s+of\s+(?:the\s+)?(?:combined|double|dual|joint))\b",
    re.IGNORECASE,
)

# Word-form numbers for common program durations (one, two, … eight).
# Checked BEFORE the digit _DURATION_VALUE_RE so "Three years full-time, with
# the possibility of a six to 12 month extension" yields (3, "Year") rather
# than (12, "Month") — the first DIGIT in the string would otherwise be "12".
_WORD_DURATION_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight)\s+(years?|months?|semesters?|trimesters?)\b",
    re.I,
)
_WORD_NUM_MAP: dict[str, float] = {
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0,
    "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0,
}
# Same patterns but ONLY used to gate the loose Pattern-2 fallback — not the
# labeled Pattern-0 match.  "Duration: 2 years (or part-time equivalent)" is
# a valid duration sentence where Pattern 0 must still fire; "Part time
# equivalent: 8 years" is a cap sentence that must be blocked for Pattern 2.
# By splitting into two uses we avoid filtering the labeled sentence while
# still blocking fallback matches on candidature/cap copy.
_DURATION_CAP_FALLBACK_RE = _DURATION_RESEARCH_CAP_RE  # alias, same object
# Bug 3 (KBS / Torrens): compound durations like "1 year, 8 months" or
# "1 year and 8 months".  The single-unit patterns above only capture the
# first numeric token ("1 Year") and discard the month component.
# This pattern captures both integers and converts to total months.
# Order of checks: compound fires BEFORE the single-unit patterns so the
# more precise value wins.  Two month representations are supported:
#   "1 year, 8 months"  /  "1 year and 8 months"  /  "1 year 8 months"
_COMPOUND_DURATION_RE = re.compile(
    r"(\d+)\s+years?\s*,?\s*(?:and\s+)?(\d+)\s+months?",
    re.IGNORECASE,
)

# Bug A (KBS grad certs): slash-structured program-info cells.
# Pages like KBS publish duration as "8 months / 4 subjects / 2 trimesters"
# where the first slash-separated token is the real duration and subsequent
# tokens are delivery structure (subject count, delivery period).
# These sentences contain NO duration-context word ("course", "full-time",
# etc.) so the loose Pattern-2 guard (_DURATION_CONTEXT) refuses to fire,
# leaving (8, Month) absent from `parsed`.  A candidature-deadline sentence
# elsewhere ("complete within 8 years") then wins the tournament and is later
# nullified by the grad-cert sanity cap, dropping the course entirely.
# Fix: detect the slash pattern at the start of a sentence and add the first
# token directly to `parsed` at Pattern-0 priority (×100) so it beats any
# fallback match for the same number in a different unit.
_SLASH_PROGRAM_STRUCTURE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(years?|months?|weeks?|semesters?|trimesters?)\s*/",
    re.IGNORECASE,
)

_UNIT_RANK = {"Year": 4, "Semester": 3, "Trimester": 3, "Month": 2, "Week": 1}
_WEEKS = {"Year": 52, "Semester": 20, "Trimester": 14, "Month": 4, "Week": 1}
# Per-unit extraction caps.  Values above these are almost certainly
# extraction errors (e.g. a year number like "2012" being mistaken for
# a duration, or a recency-window clause escaping the anti-context filter).
# "Year" cap is deliberately set to 10 to match the data-quality warning
# threshold (_DURATION_YEAR_MAX in data_quality.py); anything beyond 10 years
# triggers a suspicious_duration warning anyway, so rejecting it here avoids
# staging the bad value in the first place.
_DURATION_CAP = {"Year": 10, "Semester": 24, "Trimester": 36, "Month": 96, "Week": 416}

# Mirrors `study_mode._extract_strong_label_value`: a structural pre-pass
# that reads the value cell directly out of the DOM so the same
# flattened-text boundary-collision bug class can't bleed an adjacent
# field's value into the duration capture (e.g. an ASA-style
# `<div><strong>Location</strong></div><div>Sydney, 3 days a week</div>
# <div><strong>Duration</strong></div><div>3 years</div>` template
# where stripping tags yields a token run that the loose `<num> <unit>`
# fallback could match in the wrong cell).
_DURATION_LABEL_RE = re.compile(
    r"(?:course\s+duration|duration|course\s+length|"
    r"program(?:me)?\s+length|study\s+duration|"
    r"length\s+of\s+(?:course|program(?:me)?|study)|"
    r"full[-\s]?time\s+duration|standard\s+duration)",
    re.IGNORECASE,
)
_DURATION_VALUE_RE = re.compile(
    rf"\b(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.IGNORECASE
)
_STRONG_VALUE_CHAR_CAP = 300


def _normalise_unit(raw: str) -> str | None:
    raw = raw.lower()
    if "year" in raw or "yr" in raw:
        return "Year"
    if "month" in raw:
        return "Month"
    if "week" in raw:
        return "Week"
    if "trimester" in raw:
        return "Trimester"
    if "semester" in raw:
        return "Semester"
    return None


def _convert_weeks(amount: float, unit: str) -> tuple[float, str]:
    """Issue 4: vocational course pages express duration in weeks (e.g.
    "104 weeks") while degree pages use years natively.  Convert to a
    human-readable unit so the UI shows "2 Year" instead of "104 Week":

    * weeks ≥ 52  → years  (rounded to nearest 0.5)
    * weeks ≥ 12  → months (rounded to nearest whole month)
    * weeks < 12  → keep as weeks

    Non-Week units are returned unchanged.
    """
    if unit != "Week":
        return amount, unit
    if amount >= 52:
        years = round(amount / 52 * 2) / 2  # nearest 0.5 year
        return years, "Year"
    if amount >= 12:
        months = round(amount / 4.348)  # 1 month ≈ 4.348 weeks
        return float(months), "Month"
    return amount, unit


_PART_THEN_FULL_RE = re.compile(
    r"part[- ]?time\b.+\bfull[- ]?time\b", re.IGNORECASE | re.DOTALL
)
_FULLTIME_SPLIT_RE = re.compile(r"\bfull[- ]?time\b", re.IGNORECASE)


def _classify_duration_value(
    value: str,
    prefer_fulltime: bool = False,
) -> tuple[float, str] | None:
    """Parse a duration expression from a label-value cell. Returns
    ``(amount, canonical_unit)`` or ``None`` when no plausible
    duration is recoverable. Applies the same per-unit caps as the
    keyword fallback so junk values (e.g. "200 years") are rejected.

    Priority order:
      1. Compound digit expressions: "1 year, 8 months" → 20 Month
      2. Word-form numbers: "Three years full-time" → 3 Year
         (checked before digit scan so "three" wins over a later "12"
         in an extension clause)
      3. Digit scan — skips matches inside extension clauses
         ("possibility of a 12 month extension") so the nominal
         duration is not displaced by the extension qualifier.

    When ``prefer_fulltime=True`` and the value lists Part-time before
    Full-time (e.g. "Part-time (8 years), Full-time (4 years)"), only
    the Full-time portion is parsed.  Needed for WLV-style cells where
    the DOM value contains both modes in a single string.
    """
    if prefer_fulltime and _PART_THEN_FULL_RE.search(value):
        # Keep only the text starting from "Full-time" so the digit scan
        # sees "Full-time (4 years)" instead of "Part-time (8 years),
        # Full-time (4 years)" and returns the correct 4-year value.
        m_ft = _FULLTIME_SPLIT_RE.search(value)
        if m_ft:
            value = value[m_ft.start():]

    if _ACCELERATED.search(value):
        cleaned = compact(_ACCELERATED_DURATION_CLAUSE_RE.sub(" ", value))
        if cleaned == compact(value) or not _DURATION_VALUE_RE.search(cleaned):
            return None
        value = cleaned

    # 1. Compound match: "N year(s), M month(s)" → total months.
    cm = _COMPOUND_DURATION_RE.search(value)
    if cm:
        try:
            years = int(cm.group(1))
            months = int(cm.group(2))
            total_months = years * 12 + months
            if 0 < total_months <= _DURATION_CAP["Month"]:
                return float(total_months), "Month"
        except (ValueError, IndexError):
            pass

    # 2. Word-form number: "Three years", "one semester", etc.
    wm = _WORD_DURATION_RE.search(value)
    if wm:
        amount = _WORD_NUM_MAP.get(wm.group(1).lower())
        unit = _normalise_unit(wm.group(2))
        if amount is not None and unit and 0 < amount <= _DURATION_CAP[unit]:
            return amount, unit

    # 2b. Minimum-stated duration: "Minimum 2 years, up to a maximum of 5 years"
    # Always return the floor value — the maximum in the same string must never win.
    mm = _MINIMUM_DURATION_RE.search(value)
    if mm:
        try:
            min_amount = float(mm.group(1))
            min_unit = _normalise_unit(mm.group(2))
            if min_unit and 0 < min_amount <= _DURATION_CAP[min_unit]:
                return min_amount, min_unit
        except (ValueError, IndexError):
            pass

    # 3. Digit scan — prefer matches NOT inside an extension clause.
    # Walk all matches so we can skip "12 month" in
    # "with the possibility of a six to 12 month extension" and still
    # capture "3 years" earlier in the same value string.
    for m in _DURATION_VALUE_RE.finditer(value):
        # Check a ±40-char window around the match for "extension".
        window_start = max(0, m.start() - 40)
        window_end = min(len(value), m.end() + 40)
        window = value[window_start:window_end]
        if _EXTENSION_CTX_RE.search(window):
            continue  # part of an extension clause — skip
        try:
            amount = float(m.group(1))
        except ValueError:
            continue
        unit = _normalise_unit(m.group(2))
        if not unit:
            continue
        cap = _DURATION_CAP[unit]
        if not (0 < amount <= cap):
            continue
        return amount, unit
    return None


def _extract_strong_label_value(
    html: str,
    prefer_fulltime: bool = False,
) -> tuple[tuple[float, str] | None, str | None]:
    """Structural pre-pass for label/value duration idioms in the DOM.
    See :func:`study_mode._extract_strong_label_value` for the full
    rationale — this is the same idea, restricted to duration labels.

    Recognised idioms:

    * ``<strong>Duration</strong>`` / ``<b>Course duration:</b>`` —
      value either inline after the bold tag or in a sibling element.
      Walks forward in document order until the next labelled boundary.
    * ``<dt>Duration</dt><dd>3 years</dd>`` — definition lists.
    * ``<th>Course length</th><td>2 years</td>`` — table key/value rows.
    * ``<h4>Duration</h4> … <p>2 years full-time</p>`` — snapshot cards
      used by Southern Cross University and similar CMS templates.
    """
    if not html:
        return None, None
    try:
        from bs4 import BeautifulSoup
        from bs4.element import NavigableString, Tag
    except ImportError:  # pragma: no cover - bs4 is a hard dep
        return None, None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - defensive
        return None, None

    for label_tag in soup.find_all(
        ("strong", "b", "dt", "th", "h1", "h2", "h3", "h4", "h5", "h6")
    ):
        label_raw = label_tag.get_text(" ", strip=True).rstrip(":").strip()
        if not label_raw or not _DURATION_LABEL_RE.fullmatch(label_raw):
            continue

        value_text: str | None = None
        if label_tag.name == "dt":
            sibling = label_tag.find_next_sibling("dd")
            if sibling is not None:
                value_text = sibling.get_text(" ", strip=True)
        elif label_tag.name == "th":
            sibling = label_tag.find_next_sibling("td")
            if sibling is not None:
                value_text = sibling.get_text(" ", strip=True)
        else:
            parts: list[str] = []
            char_count = 0
            for node in label_tag.next_elements:
                if isinstance(node, Tag):
                    if node is label_tag:
                        continue
                    if node.name in ("strong", "b", "h1", "h2", "h3",
                                     "h4", "h5", "h6", "dt", "th",
                                     "tr"):
                        break
                    continue
                if isinstance(node, NavigableString):
                    text = str(node).strip()
                    if not text:
                        continue
                    parts.append(text)
                    char_count += len(text) + 1
                    if char_count >= _STRONG_VALUE_CHAR_CAP:
                        break
            value_text = " ".join(parts)

        if not value_text:
            continue
        value_text = value_text.lstrip(":-– ").strip()
        if not value_text:
            continue
        parsed = _classify_duration_value(value_text, prefer_fulltime=prefer_fulltime)
        # Some SSR summary cards use an explicit "Full time duration" label
        # but render only the numeric year count in the value cell
        # (for example, <dt>Full time duration</dt><dd>4</dd>).  The label
        # supplies the missing semantics, so do not fall through to a broad
        # page-text scan that can capture unrelated "2 years" admission prose.
        if (
            parsed is None
            and re.fullmatch(r"full[-\s]?time\s+duration", label_raw, re.I)
            and re.fullmatch(r"\d+(?:\.\d+)?", value_text)
        ):
            amount = float(value_text)
            if 0 < amount <= _DURATION_CAP["Year"]:
                parsed = (amount, "Year")
        if parsed is not None:
            snippet = (
                f"<{label_tag.name}>{label_raw}</{label_tag.name}> -> "
                f"{value_text[:80]}"
            )
            return parsed, snippet
    return None, None


def _from_duration_meta(html: str) -> tuple[tuple[float, str], str] | None:
    """Read canonical course duration from a dedicated meta tag.

    UOW's static HTML publishes the current course's value as
    ``<meta name="duration" content="2 years full-time, ...">`` even when the
    visible summary panel needs JavaScript. Recommendation cards later in the
    same HTML contain other courses' durations, so this machine-readable value
    must take precedence over every DOM/text fallback.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - defensive
        return None

    for tag in soup.find_all("meta"):
        name = str(tag.get("name") or "").strip().lower()
        if name != "duration":
            continue
        content = compact(str(tag.get("content") or ""))
        if not content:
            continue
        parsed = _classify_duration_value(content)
        if parsed is not None:
            return parsed, f'meta[name="duration"]: {content[:120]}'
    return None


def _strip_related_course_sections(html: str) -> str:
    """Remove high-confidence sibling-course recommendation containers."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - defensive
        return html

    matched_containers = []
    for tag in soup.find_all(True):
        marker = " ".join(
            [
                str(tag.get("id") or ""),
                *[str(value) for value in (tag.get("class") or [])],
            ]
        )
        if not marker or not _RELATED_COURSE_CONTAINER_RE.search(marker):
            continue
        matched_containers.append(tag)

    # Decompose only after discovery. Decomposing while iterating invalidates
    # descendant Tag objects (their attrs become None) and can make a later
    # ``tag.get(...)`` call fail.
    for tag in matched_containers:
        if tag.parent is not None:
            tag.decompose()

    for heading in list(soup.find_all(("h2", "h3", "h4", "h5", "h6"))):
        if heading.parent is None:
            continue
        label = compact(heading.get_text(" ", strip=True))
        if not _RELATED_COURSE_HEADING_RE.fullmatch(label):
            continue
        container = heading.find_parent(("section", "aside"))
        if container is not None:
            container.decompose()

    return str(soup)


# ── QUT structural reader (qut.edu.au) ─────────────────────────────────────
# QUT's per-course pages are Cloudflare-walled and JS-rendered.  The rendered
# sidebar "Explore this course" panel exposes the international duration in
# `<ul data-course-map-key="quickBoxDurationINT">` with a single `<li>` value
# (e.g. "24 weeks full-time", "1.5 years full-time").  The same UL family
# also publishes `quickBoxDurationINTFt` / `quickBoxDurationDOMFt` variants
# for the full-time-only breakdown, but the canonical INT entry is the
# preferred source because it matches what the live page shows international
# applicants.
#
# Without this structural reader, the generic regex cascade reads the
# course description prose and lifts a stray "5.5" or "Year 1, Year 2"
# token instead of the canonical "24 weeks full-time" value, and the
# arbiter rejects the result (selected=false) so `duration` stages NULL.
# Verified 2026-05-17 on Graduate Diploma in Legal Practice
# (university_id QUT, course id 22761).
def _from_qut_quickbox(html: str, url: str) -> tuple[tuple[float, str], str] | None:
    from urllib.parse import urlparse as _urlparse
    host = (_urlparse(url or "").hostname or "").lower()
    if not (host == "qut.edu.au" or host.endswith(".qut.edu.au")):
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    selected = None
    selected_key = ""
    # Preference order: INT (canonical international value) → INTFt
    # (full-time-only INT variant) → DOM (last-resort domestic value).
    for preferred in (
        "quickBoxDurationINT",
        "quickBoxDurationINTFt",
        "quickBoxDurationDOM",
    ):
        for el in soup.find_all(attrs={"data-course-map-key": True}):
            key = el.get("data-course-map-key") or ""
            # Exact-match — substring (`preferred in key`) would let
            # `quickBoxDurationINTFt` satisfy the `quickBoxDurationINT`
            # pass and break the intended INT > INTFt > DOM precedence
            # whenever the Ft variant appears earlier in the DOM.
            if not isinstance(key, str) or key.strip() != preferred:
                continue
            # Allow either a UL/OL with LIs OR a leaf element holding the
            # raw text — QUT uses both shapes across course pages.
            texts: list[str] = []
            lis = el.find_all("li") if el.name in ("ul", "ol") else []
            if lis:
                for li in lis:
                    t = compact(li.get_text(" ", strip=True))
                    if t:
                        texts.append(t)
            else:
                t = compact(el.get_text(" ", strip=True))
                if t:
                    texts.append(t)
            if texts:
                selected = texts[0]  # canonical single-value entry
                selected_key = key
                break
        if selected is not None:
            break
    if not selected:
        return None
    parsed = _classify_duration_value(selected)
    if parsed is None:
        return None
    return parsed, f"qut.quickbox[{selected_key}]: {selected[:80]}"


async def extract(html: str, url: str) -> list[ExtractionResult]:
    # Per-uni: load reject_sentence_patterns from the contextvar set by
    # set_uni_config() before extraction runs.  These are compiled once
    # here and applied in two places below:
    #   1. Structural pre-pass — if the DOM-cell value matches, we fall
    #      through to the sentence tournament instead of returning early.
    #   2. Sentence tournament — any sentence matching a pattern is skipped
    #      entirely (same effect as the global _ACCELERATED filter).
    _uni_reject_pats: list[re.Pattern[str]] = []
    _prefer_fulltime: bool = False
    try:
        from app.services.scraper.config.context import get_uni_config
        _dcfg = get_uni_config().extraction.text_cleaning.duration
        for _p in _dcfg.reject_sentence_patterns:
            try:
                _uni_reject_pats.append(re.compile(_p, re.IGNORECASE))
            except re.error:
                pass  # invalid pattern in YAML — skip silently
        _prefer_fulltime = _dcfg.prefer_fulltime
    except Exception:
        pass  # contextvar not set or config missing — treat as empty list

    # AUT points metadata is more reliable than its duration metadata. A
    # 180-point programme is 1.5 full-time years even when a stale duration
    # meta tag says one year.
    aut_points = _from_aut_points(html, url)
    if aut_points is not None:
        amount, snippet = aut_points
        return [
            ExtractionResult(
                field_key="duration",
                value=amount,
                normalized={"duration": amount, "duration_term": "Year"},
                confidence=0.99,
                snippet=snippet,
                method="duration.aut_points",
            )
        ]

    # Canonical machine-readable page metadata wins before any recommendation
    # cards or flattened prose can enter the duration tournament.
    meta = _from_duration_meta(html)
    if meta is not None:
        (amount, unit), snippet = meta
        amount, unit = _convert_weeks(amount, unit)
        return [
            ExtractionResult(
                field_key="duration",
                value=amount,
                normalized={"duration": amount, "duration_term": unit},
                confidence=0.95,
                snippet=snippet,
                method="duration.meta",
            )
        ]

    # Generic safety net for pages without canonical metadata. Intake and
    # study-mode extraction already exclude sibling-course widgets for the same
    # reason; duration must not let those cards participate either.
    html = _strip_related_course_sections(html)

    # QUT structural pre-pass — see _from_qut_quickbox above.  Runs BEFORE
    # the generic structural/regex cascade because QUT pages have no
    # `<strong>Duration</strong>` / `<dl>` / `<table>` duration cell that
    # the generic pre-pass can latch onto; reading the canonical
    # `quickBoxDurationINT` UL directly is the only way to surface "24
    # weeks full-time" instead of the unrelated prose tokens the regex
    # tournament otherwise rejects.
    qut = _from_qut_quickbox(html, url)
    if qut is not None:
        (amount, unit), snippet = qut
        amount, unit = _convert_weeks(amount, unit)
        return [
            ExtractionResult(
                field_key="duration",
                value=amount,
                normalized={"duration": amount, "duration_term": unit},
                confidence=0.95,
                snippet=snippet,
                method="duration.qut_quickbox",
            )
        ]
    # Structural pre-pass FIRST — see _extract_strong_label_value for
    # the rationale. When the page publishes duration as a
    # `<strong>Duration</strong>` / `<dt>/<dd>` / `<th>/<td>` pair,
    # read the value cell out of the DOM directly so a flattened-text
    # boundary collision with the previous field's value can't bleed
    # an unrelated `<num> <unit>` token run into the duration capture.
    structural, snippet = _extract_strong_label_value(html, prefer_fulltime=_prefer_fulltime)
    # Per-uni: if the structural DOM-cell text matches a configured reject
    # pattern (e.g. "up to 10 years" = max-completion-time, not standard
    # duration), discard the structural result and fall through to the
    # sentence tournament so the real labeled duration can be found instead.
    if structural is not None and _uni_reject_pats:
        raw_cell = snippet or ""
        if any(_pat.search(raw_cell) for _pat in _uni_reject_pats):
            structural = None
            snippet = None
    if structural is not None:
        amount, unit = structural
        amount, unit = _convert_weeks(amount, unit)  # Issue 4: week→year/month
        struct_years = amount * _WEEKS.get(unit, 52) / 52

        if struct_years >= 0.5:
            # Structural result ≥ 6 months — treat as a real program duration.
            # Check the page prose for a "Minimum N years" qualifier that's
            # *lower* than the structural value.  UTAS flex-enrolment pages
            # put the enrollment cap (e.g. "5 years") in the Duration DOM
            # cell, while the real program floor ("Minimum 2 years, up to a
            # maximum of 5 years") lives in the body.  When that pattern
            # exists and the prose minimum is ≥ 0.5 years (i.e. it's a
            # genuine program-length floor, not an enrolment-period floor),
            # prefer the prose minimum.
            # NOTE: threshold was 1.5 years — Graduate Certificates (8 months,
            # 0.615 years) were incorrectly falling into the sub-year floor
            # path and losing the structural method tag.  0.5 years (6 months)
            # is the right floor; anything below that is an enrollment-period
            # minimum ("1 semester"), not a program length.
            prose_text = compact(html_to_text(html))
            prose_min_m = _MINIMUM_DURATION_RE.search(prose_text or "")
            if prose_min_m:
                try:
                    pa = float(prose_min_m.group(1))
                    pu = _normalise_unit(prose_min_m.group(2))
                    if pu:
                        py = pa * _WEEKS.get(pu, 52) / 52
                        if struct_years > py >= 0.5:
                            amount, unit = pa, pu
                            snippet = f"prose-minimum: {prose_min_m.group(0)}"
                except (ValueError, IndexError):
                    pass
            return [
                ExtractionResult(
                    field_key="duration",
                    value=amount,
                    normalized={"duration": amount, "duration_term": unit},
                    confidence=0.85,
                    snippet=snippet,
                    method="duration.structural",
                )
            ]
        # else: struct_years < 0.5 — the structural value is a sub-year
        # enrolment floor ("Minimum 1 Semester, up to a maximum of 4 years").
        # Do NOT return early.  Add it to parsed at ×1 priority (lowest) so
        # the sentence loop can find the real program duration from the prose
        # (e.g. "3 years full-time") and beat it in the weight tournament.
        struct_weeks = amount * _WEEKS.get(unit, 1)
        parsed_sub_year: list[tuple[float, float, str, str]] = [
            (
                (struct_weeks * 100 + _UNIT_RANK.get(unit, 1)) * 1.0,
                amount,
                unit,
                f"structural (sub-year floor): {snippet}",
            )
        ]
    else:
        parsed_sub_year = []

    # Use the raw html_to_text output (before compact) so that newlines
    # emitted for block-level tags (dt, dd, p, div, …) survive into the
    # sentence splitter.  compact() is applied per-sentence afterwards so
    # each candidate is still normalised.
    raw_text = html_to_text(html)
    if not raw_text.strip():
        return []
    text = compact(raw_text)  # kept for prose_text / other callers

    # Build candidate sentences (skip accelerated callouts entirely).
    sentences = [
        compact(s)
        for s in re.split(r"(?<=[.!?])\s+|\n", raw_text)
        if s.strip()
    ]
    # Seed parsed with any sub-year structural candidate found above so it
    # participates in the tournament at ×1 priority (lowest).
    parsed: list[tuple[float, float, str, str]] = list(parsed_sub_year)
    for s in sentences:
        if _ACCELERATED.search(s):
            cleaned = compact(_ACCELERATED_DURATION_CLAUSE_RE.sub(" ", s))
            if cleaned == compact(s) or not _DURATION_VALUE_RE.search(cleaned):
                continue
            s = cleaned
        # Per-uni: strip reject_sentence_pattern fragments from the sentence
        # before it enters the tournament.  We strip rather than skip so that
        # a page where "up to N years" and "M year program" appear in the same
        # text block (e.g. when html_to_text collapses multiple block elements
        # into one long line) still extracts the correct M-year duration.
        # Example: "Duration up to 10 years The Doctor of Medicine is a 4 year
        #            program..." → strip "up to 10 years" → "4 year" wins.
        if _uni_reject_pats:
            for _rp in _uni_reject_pats:
                s = _rp.sub("", s)
            s = re.sub(r"\s{2,}", " ", s).strip()
            if not s:
                continue
        # Flag sentences about research degree candidature caps, part-time
        # equivalents, and research periods.  These have large numbers
        # (e.g. "maximum candidature: 8 years", "part-time equivalent: 8 years")
        # that must NOT become the program duration.
        # Previously this was a hard continue — but "Duration: 2 years
        # (or part-time equivalent)" is a VALID sentence where Pattern 0
        # (explicit label) should still fire.  We therefore only block the
        # loose Pattern-2 fallback for cap sentences; Patterns 0/1 are
        # allowed through so the label match wins the weight tournament.
        is_cap_sentence = bool(_DURATION_RESEARCH_CAP_RE.search(s))
        # Skip sentences that are talking about credit-point structure rather
        # than program duration — see _CREDIT_POINT_CONTEXT comment.
        credit_context = bool(_CREDIT_POINT_CONTEXT.search(s))
        # PR-1.5: pre-compute duration / anti-duration context so the loose
        # pattern-3 fallback can gate on them. Patterns 1 and 2 already
        # have their own context (duration label / "full-time"), so they
        # don't need either gate.
        duration_context = bool(_DURATION_CONTEXT.search(s))
        anti_duration_context = bool(_DURATION_ANTI_CONTEXT.search(s))
        # Demote combined/add-on degree sentences so the main program's
        # "Duration: N years" label always wins (see _COMBINED_DEGREE_CONTEXT_RE).
        is_combined_degree_sentence = bool(_COMBINED_DEGREE_CONTEXT_RE.search(s))
        # Sentences describing field placement / practicum hours must not
        # contribute Pattern-1 or Pattern-2 matches (see _PLACEMENT_CONTEXT_RE).
        # Exception: strip optional-year extension clauses first (e.g.
        # ", or 5 if a work placement is chosen") — these mention "placement"
        # but only as an optional additional year, not as placement hours.
        # Stripping them before the check preserves Pattern-1 for the labeled
        # full-time duration that precedes the clause.
        _s_placement_check = _PLACEMENT_OPT_YEAR_RE.sub("", s)
        is_placement_sentence = bool(_PLACEMENT_CONTEXT_RE.search(_s_placement_check))

        # "Minimum N years, up to a maximum of M years" — always use the floor.
        # Add at Pattern-0 priority (×100) and skip remaining patterns for this
        # sentence so the larger maximum value can never beat the minimum in the
        # weight tournament, regardless of which word has more context nearby.
        # Gate on is_placement_sentence too: "Minimum 16 weeks of full-time
        # placement" would otherwise extract 16 weeks as a floor duration.
        #
        # Sub-1.5-year-equivalent minimums ("Minimum 1 Semester", "Minimum 1 Year")
        # are UTAS flexible-enrolment FLOORS, not program lengths.  Skip the entire
        # sentence when the minimum is sub-1.5-year so neither the floor value nor
        # the "up to a maximum of N years" clause can enter the tournament — the
        # real program duration must come from a different prose sentence.
        min_m = _MINIMUM_DURATION_RE.search(s)
        if (
            min_m
            and not credit_context
            and not is_placement_sentence
            and not anti_duration_context
        ):
            try:
                min_amount = float(min_m.group(1))
                min_unit = _normalise_unit(min_m.group(2))
                if min_unit and 0 < min_amount <= _DURATION_CAP[min_unit]:
                    min_years_eq = min_amount * _WEEKS[min_unit] / 52
                    if min_years_eq >= 1.5:
                        # Meaningful floor (≥ 1.5 years) — add at Pattern-0
                        # priority (×100) and skip this sentence so the larger
                        # "maximum of N years" clause cannot win the tournament.
                        min_weeks = min_amount * _WEEKS[min_unit]
                        parsed.append((
                            (min_weeks * 100 + _UNIT_RANK[min_unit]) * 100.0,
                            min_amount,
                            min_unit,
                            s.strip()[:240],
                        ))
                        continue
                    # Sub-1.5-year minimum ("Minimum 1 Semester", "Minimum 1 Year"):
                    # this is a UTAS flexible-enrolment FLOOR, not a program length.
                    # Strategy:
                    #  1. Add the minimum itself at ×0.1 — pure last-resort fallback
                    #     (used when nothing else exists, e.g. genuine 1-semester
                    #     grad cert with no other prose).
                    #  2. If there is a paired "up to a maximum of X" clause, add X
                    #     at ×0.5 — still lower than any prose pattern (Pattern-1
                    #     ×10, Pattern-2 ×1) but higher than the minimum seed.
                    #     This makes grad diplomas with "min 1 sem, max 1 yr" show
                    #     1 Year when there is no separate prose sentence.
                    #  3. Strip both clauses so the normal pattern loop below cannot
                    #     re-match them at Pattern-2 (×1) priority, which would
                    #     otherwise beat the maximum seed and confuse the tournament.
                    #  Prose sentences (e.g. "3 years full-time" at Pattern-1 ×10)
                    #  always beat both seeds — bachelor programs remain correct.
                    min_weeks_seed = min_amount * _WEEKS[min_unit]
                    parsed.append((
                        (min_weeks_seed * 100 + _UNIT_RANK[min_unit]) * 0.1,
                        min_amount,
                        min_unit,
                        min_m.group(0).strip()[:240],
                    ))
                    max_m2 = re.search(
                        r"\bup\s+to\s+a?\s*maximum\s+of\s+(\d+(?:\.\d+)?)\s*"
                        r"(years?|months?|weeks?|semesters?|trimesters?)\b",
                        s,
                        re.IGNORECASE,
                    )
                    if max_m2:
                        try:
                            _mx_a = float(max_m2.group(1))
                            _mx_u = _normalise_unit(max_m2.group(2))
                            if _mx_u and 0 < _mx_a <= _DURATION_CAP[_mx_u]:
                                _mx_w = _mx_a * _WEEKS[_mx_u]
                                parsed.append((
                                    (_mx_w * 100 + _UNIT_RANK[_mx_u]) * 0.5,
                                    _mx_a,
                                    _mx_u,
                                    max_m2.group(0).strip()[:240],
                                ))
                        except (ValueError, IndexError):
                            pass
                    s = _MINIMUM_DURATION_RE.sub("", s, count=1)
                    s = re.sub(
                        r"\bup\s+to\s+a?\s*maximum\s+of\s+\d+(?:\.\d+)?\s*"
                        r"(?:years?|months?|weeks?|semesters?|trimesters?)\b",
                        "",
                        s,
                        flags=re.IGNORECASE,
                    )
                    # Fall through with cleaned sentence.
            except (ValueError, IndexError):
                pass

        # Bug A (KBS grad certs): slash-structured program-info cell.
        # "8 months / 4 subjects / 2 trimesters" — first token is real duration.
        # No duration-context word exists so Pattern-2 is blocked; add the first
        # token directly at Pattern-0 priority (×100) before anything else fires.
        slash_m = _SLASH_PROGRAM_STRUCTURE_RE.match(s)
        if slash_m and not _ACCELERATED.search(s) and not credit_context:
            try:
                _sl_amount = float(slash_m.group(1))
                _sl_unit = _normalise_unit(slash_m.group(2))
                if _sl_unit and 0 < _sl_amount <= _DURATION_CAP[_sl_unit]:
                    _sl_weeks = _sl_amount * _WEEKS[_sl_unit]
                    parsed.append((
                        (_sl_weeks * 100 + _UNIT_RANK[_sl_unit]) * 100.0,
                        _sl_amount,
                        _sl_unit,
                        s.strip()[:240],
                    ))
                    continue  # first token is definitive; skip other patterns
            except (ValueError, IndexError):
                pass

        # Bug 3: compound "N year(s), M month(s)" match — check before the
        # single-unit patterns so "1 year, 8 months" → 20 months, not "1 Year".
        # Compound matches always have duration context (both units present) so
        # no additional gate is needed.
        cm = _COMPOUND_DURATION_RE.search(s)
        if cm and not credit_context:
            try:
                c_years = int(cm.group(1))
                c_months = int(cm.group(2))
                total_months = c_years * 12 + c_months
                if 0 < total_months <= _DURATION_CAP["Month"]:
                    weeks = total_months * _WEEKS["Month"]
                    parsed.append((
                        (weeks * 100 + _UNIT_RANK["Month"]) * 1.5,  # boost over single-unit
                        float(total_months),
                        "Month",
                        s.strip()[:240],
                    ))
                    continue  # don't also try single-unit patterns on this sentence
            except (ValueError, IndexError):
                pass

        for pat_idx, pat in enumerate(_PATTERNS):
            m = pat.search(s)
            if not m:
                continue
            # Pattern 3 (loose `<num> <unit>` fallback) is the source of
            # false positives like "10 years experience" → duration=10.
            # Also block it for candidature-cap sentences (see is_cap_sentence).
            # Demand a positive duration-context word in the same
            # sentence AND no anti-context. Patterns 0 and 1 are already
            # context-bound and unaffected.
            if pat_idx == 2 and (is_cap_sentence or not duration_context or anti_duration_context):
                continue
            # Block Pattern-1 (full-time anchor) and Pattern-2 (loose fallback)
            # for sentences about field placement / practicum.  Pattern-0
            # (explicit duration label) is still allowed so that a page which
            # writes "Duration: 4 years (includes 16 weeks full-time placement)"
            # still extracts the labeled value correctly.
            if is_placement_sentence and pat_idx in (1, 2):
                continue
            try:
                amount = float(m.group(1))
            except ValueError:
                continue
            unit = _normalise_unit(m.group(2))
            if not unit:
                continue
            # Demote (don't drop) credit-point sentences so a real
            # duration sentence elsewhere wins, but if the page only ever
            # mentions duration in a credit-point sentence we still emit
            # something rather than nothing.
            weight_mod = 0.01 if credit_context else 1.0
            # Demote combined/add-on degree sentences heavily (0.001×).
            # Pattern-0 priority (×100) still means a combined-degree
            # sentence's labeled duration beats a bare fallback match from
            # the main program — but any Pattern-0 hit on the main degree
            # (not a combined-degree sentence) will win by ×1000.
            if is_combined_degree_sentence:
                weight_mod *= 0.001
            # prefer_fulltime (YAML flag): demote Pattern-0/3 label matches
            # where "part-time" appears BEFORE the matched number in the same
            # sentence.  Needed for universities (e.g. WLV) whose course-length
            # cell lists Part-time first:
            #   "Course length: Part-time (8 years), Full-time (4 years)"
            # Pattern-0 fires on "8 years" (first number) at ×100 priority.
            # Pattern-1 fires on "4 years" (after "full-time") at ×10.
            # Without this demote, Pattern-0 always wins → 8 years staged.
            # Demoting ×0.001 drops the Part-time Pattern-0 score below
            # the Pattern-1 full-time score, so "4 years" wins correctly.
            if _prefer_fulltime and pat_idx in (0, 3) and not is_combined_degree_sentence:
                _m_start = m.start()
                _pre_match = s[:_m_start]
                if re.search(r"\bpart[- ]?time\b", _pre_match, re.IGNORECASE):
                    weight_mod *= 0.001
            # Demote part-time-only loose-fallback matches (Pattern-2, ×0.1).
            # When a course page lists two separate lines:
            #   "3 years full-time"  and  "6 years part-time"
            # Pattern-2 fires on both; without this demote the larger number
            # (6) wins even though the canonical program length is the
            # full-time value.  Only Pattern-2 (loose fallback) is demoted —
            # Pattern-0 (labeled "Duration: N years") and Pattern-1
            # ("full-time N years" label-before-number) are unaffected.
            if pat_idx == 2 and not is_combined_degree_sentence:
                sentence_has_parttime = bool(
                    re.search(r"\bpart[- ]?time\b", s, re.IGNORECASE)
                )
                sentence_has_fulltime = bool(
                    re.search(r"\bfull[- ]?time\b", s, re.IGNORECASE)
                )
                if sentence_has_parttime and not sentence_has_fulltime:
                    weight_mod *= 0.1
            # Cap depending on unit so we reject only true outliers
            # (e.g. "120 weeks" is 2 years, fine; "200 years" is junk).
            cap = {"Year": 12, "Semester": 24, "Trimester": 36, "Month": 96, "Week": 416}[unit]
            if not (0 < amount <= cap):
                continue
            # Pattern-priority boost: explicit duration-label matches
            # (Pattern 0) must always beat loose fallback matches (Pattern 2)
            # regardless of the numeric values involved.
            # Without this boost, "8 years" (pattern-2, weight=41604) would
            # beat "Duration: 2 years" (pattern-0, weight=10404) in the
            # weight tournament — exact failure mode on UniSQ MRes page.
            # Pattern 0 → ×100 (labeled), Pattern 1 → ×10 (full-time),
            # Pattern 2 → ×1 (fallback).
            # Pattern 3 (JSON-shape, e.g. `"heading":"Duration","summary":"3 years"`)
            # carries an explicit label and is as trustworthy as Pattern 0,
            # so it gets the same ×100 priority boost.  Pattern 2 (loose
            # bare-number fallback) keeps ×1.
            pattern_priority = (
                100.0 if pat_idx in (0, 3)
                else (10.0 if pat_idx == 1 else 1.0)
            )
            weeks = amount * _WEEKS[unit]
            parsed.append((
                (weeks * 100 + _UNIT_RANK[unit]) * weight_mod * pattern_priority,
                amount,
                unit,
                s.strip()[:240],
            ))
            break  # one match per sentence is enough

    if not parsed:
        return []
    parsed.sort(key=lambda t: t[0], reverse=True)
    _, amount, unit, snippet = parsed[0]

    # ── Same-N cross-check (KBS / Torrens grad cert bug) ─────────────────────
    # If the tournament winner is (N, "Year") for N ≥ 5, and the same integer N
    # also appears as a "Month" candidate, prefer Month.  Course pages frequently
    # show "N months" (real duration) alongside "complete/enrolled within N years"
    # (candidature deadline).  The deadline sentence wins the weight tournament
    # even when the anti-context guards fire because deadline phrasings vary
    # widely ("maximum enrolment duration", "must finish by", "no longer than",
    # etc.).  For N ≥ 5, "N months" is a plausible grad-cert duration while
    # "N years" is not — no accredited Australian grad cert runs for 5+ years.
    if unit == "Year" and 5.0 <= amount <= 24.0:
        _month_same_n = next(
            (p for p in parsed if p[2] == "Month" and p[1] == amount), None
        )
        if _month_same_n is not None:
            _, amount, unit, snippet = _month_same_n

    amount, unit = _convert_weeks(amount, unit)  # Issue 4: week→year/month
    return [
        ExtractionResult(
            field_key="duration",
            value=amount,
            normalized={"duration": amount, "duration_term": unit},
            confidence=0.75,
            snippet=snippet,
            method="regex",
        )
    ]
