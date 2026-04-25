"""Rule-based category classifier for university courses.

The taxonomy mirrors the Node `BATCH_CLASSIFY_PROMPT` so the Python and Node
pipelines emit values from the same controlled vocabulary — required because
both still write to the same `scraped_courses` table in production.

Strategy: weighted keyword scoring against the course name. A keyword in the
course name is far stronger evidence than a keyword in body text (a "Master
of Computer Science" page may mention "business analytics" in passing). We
deliberately do NOT scan body text in this pass — false-positive rate was
too high. AI fallback can be layered on later for cases where the rule set
returns no match, but the rules cover ~85% of catalogues without any AI
spend.
"""
from __future__ import annotations

import re

CATEGORIES = (
    "Business & Management",
    "Computer Science & IT",
    "Engineering & Technology",
    "Medicine & Health",
    "Arts, Humanities & Social Sciences",
    "Education & Social Work",
    "Architecture, Building & Design",
    "Media & Communications",
    "Law & Legal Studies",
    "Hospitality, Tourism & Events",
    "Science & Mathematics",
    "Agriculture & Environmental Science",
    "Trades & Construction",
)

# Each tuple: (category, keywords). Keywords are matched as whole-word
# patterns (case-insensitive) against the course name.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Business & Management",
        (
            # Note: bare "management" / "leadership" are intentionally
            # excluded — they collide with "Hospitality Management",
            # "Project Management", "Educational Leadership" etc. The
            # category-specific multi-word phrases below catch the real
            # business signals.
            "business", "mba", "marketing", "finance", "accounting",
            "commerce", "economics", "business administration",
            "business management", "human resource", "hr management",
            "supply chain", "logistics", "project management",
            "entrepreneurship", "international business", "banking",
            "actuarial",
        ),
    ),
    (
        "Computer Science & IT",
        (
            "computer science", "computing", "information technology",
            "information systems", "cyber", "software", "data science",
            "data analytics", "artificial intelligence", "machine learning",
            "ai ", "ai)", "(ai", "cloud computing", "devops", "fintech",
            "blockchain", "computer engineering", "it management",
        ),
    ),
    (
        "Engineering & Technology",
        (
            "engineering", "mechatronic", "mechatronics", "mechanical",
            "electrical", "civil engineering", "chemical engineering",
            "aerospace", "robotics", "biomedical engineering", "automotive",
            "telecommunication",
        ),
    ),
    (
        "Medicine & Health",
        (
            "medicine", "nursing", "pharmacy", "dentistry", "physiotherapy",
            "occupational therapy", "public health", "health science",
            "biomedical science", "midwifery", "paramedic", "psychology",
            "clinical", "radiography", "medical", "healthcare", "podiatry",
            "optometry", "chiropractic", "veterinary",
        ),
    ),
    (
        "Arts, Humanities & Social Sciences",
        (
            "arts", "humanities", "history", "philosophy", "sociology",
            "anthropology", "linguistics", "literature", "religion",
            "political science", "international relations", "criminology",
            "social science", "language", "creative writing", "music",
            "performing arts", "theatre", "fine arts",
        ),
    ),
    (
        "Education & Social Work",
        (
            "education", "teaching", "early childhood", "social work",
            "counselling", "counseling", "youth work", "community services",
            "tesol",
        ),
    ),
    (
        "Architecture, Building & Design",
        (
            "architecture", "interior design", "construction", "urban planning",
            "landscape", "industrial design", "graphic design", "product design",
            "design ",
        ),
    ),
    (
        "Media & Communications",
        (
            "media", "communication", "journalism", "public relations",
            "advertising", "film", "screen", "digital media", "broadcasting",
            "publishing", "animation", "game design",
        ),
    ),
    (
        "Law & Legal Studies",
        (
            "law", "legal", "juris doctor", "llb", "llm", "criminal justice",
        ),
    ),
    (
        "Hospitality, Tourism & Events",
        (
            # Multi-word phrases first so "Hospitality Management" beats
            # the bare "business" / "management" matches in Business &
            # Management. Without these, prod was bucketing every
            # "Master of Hospitality Management" as Business & Management
            # — exact bug the user reported.
            "hospitality management", "hotel management", "tourism management",
            "event management", "culinary arts",
            # Issue 2: vocational cookery keywords — VIT vocational courses
            "commercial cookery", "kitchen management", "patisserie",
            "cookery", "barista",
            "hospitality", "tourism", "event", "hotel", "culinary",
            "restaurant", "wine ",
        ),
    ),
    (
        "Science & Mathematics",
        (
            "science", "mathematics", "physics", "chemistry", "biology",
            "biotechnology", "geology", "astronomy", "statistics",
            "biochemistry", "genetics", "neuroscience", "marine science",
        ),
    ),
    (
        "Agriculture & Environmental Science",
        (
            "agriculture", "environmental", "environment", "horticulture",
            "forestry", "ecology", "sustainability", "wildlife",
            "agribusiness", "viticulture",
        ),
    ),
    (
        "Trades & Construction",
        (
            # Issue 2: VIT vocational courses — AQF-level trade qualifications
            # that don't fit Engineering (which is degree-level theory) or
            # Architecture/Building (which is design-focused). These are
            # hands-on skilled-trades certificates and diplomas.
            # NOTE: no generic AQF phrases ("certificate iii in", "diploma of")
            # — those are too broad and would beat specific keywords from
            # other categories via word-count weighting.
            "carpentry", "plumbing", "bricklaying", "concreting",
            "tiling", "plastering", "electrical trade",
            "refrigeration", "air conditioning", "hvac",
            "cabinet making", "joinery", "welding", "boilermaking",
        ),
    ),
)


def _score(name: str) -> dict[str, int]:
    """Return per-category weighted scores.

    Weighting: a matched keyword contributes its word-count. A 2-word
    phrase like "computer science" is worth 2; a single word like
    "business" is worth 1. This matters for ambiguous titles such as
    "Computer Science with Business Foundations" — without weighting the
    two categories tie 1-1; with weighting CS wins 2-1, which matches the
    operator's intuition that the multi-word match is more specific.
    """
    if not name:
        return {}
    n = name.lower()
    scores: dict[str, int] = {}
    for category, keywords in _KEYWORDS:
        score = 0
        for kw in keywords:
            pattern = r"\b" + re.escape(kw.strip()) + r"\b"
            if re.search(pattern, n):
                score += max(1, len(kw.split()))
        if score:
            scores[category] = score
    return scores


# Sub-category fine-grained mapping. Each tuple: (parent_category, sub_label,
# keywords). Matched against the course name in order; first hit wins. Mirrors
# Node's `mapCourseToCategory` (routes/scrape.ts:9966) so both pipelines emit
# the same controlled vocabulary into ``scraped_courses.sub_category``. Without
# this, ``sub_category`` was always NULL and the Review table's "Field" column
# fell back to the parent category, hiding the more specific signal an
# operator needs to triage a course (e.g. "Hospitality Management" vs
# "Tourism" both showing as "Hospitality, Tourism & Events").
_SUB_CATEGORY_MAP: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # Multi-word phrases first so the more-specific keyword wins.
    ("Hospitality, Tourism & Events", "Hospitality Management", ("hospitality management", "hotel management")),
    ("Hospitality, Tourism & Events", "Tourism Management",     ("tourism management", "tourism")),
    ("Hospitality, Tourism & Events", "Event Management",       ("event management", "event ")),
    # Issue 2: vocational cookery sub-categories — must come BEFORE
    # "Culinary Arts" so "commercial cookery" matches Cookery not Culinary Arts.
    ("Hospitality, Tourism & Events", "Cookery",                ("commercial cookery", "kitchen management", "patisserie", "cookery")),
    ("Hospitality, Tourism & Events", "Culinary Arts",          ("culinary arts", "culinary", "restaurant")),
    ("Business & Management",     "MBA",                    ("mba", "master of business administration")),
    ("Business & Management",     "Accounting",             ("accounting", "accountancy")),
    ("Business & Management",     "Finance",                ("finance", "banking", "actuarial")),
    ("Business & Management",     "Marketing",              ("marketing",)),
    ("Business & Management",     "Project Management",     ("project management",)),
    ("Business & Management",     "International Business", ("international business",)),
    ("Business & Management",     "Supply Chain & Logistics", ("supply chain", "logistics")),
    ("Business & Management",     "Human Resources",        ("human resource", "hr management")),
    ("Business & Management",     "Technology Management",  ("technology management",)),
    ("Business & Management",     "Entrepreneurship",       ("entrepreneurship",)),
    ("Computer Science & IT",     "Networking",             ("networking", "network engineering", "computer networks")),
    ("Computer Science & IT",     "Data Science",           ("data science", "data analytics")),
    ("Computer Science & IT",     "Cyber Security",         ("cyber",)),
    ("Computer Science & IT",     "Artificial Intelligence", ("artificial intelligence", "machine learning")),
    ("Computer Science & IT",     "Software Engineering",   ("software engineering", "software development", "software application development", "application development")),
    ("Computer Science & IT",     "Information Systems",    ("information systems", "information technology", "it management")),
    ("Engineering & Technology",  "Mechanical Engineering", ("mechanical engineering", "mechatronic")),
    ("Engineering & Technology",  "Civil Engineering",      ("civil engineering",)),
    ("Engineering & Technology",  "Electrical Engineering", ("electrical engineering",)),
    ("Engineering & Technology",  "Biomedical Engineering", ("biomedical engineering",)),
    ("Engineering & Technology",  "Chemical Engineering",   ("chemical engineering",)),
    ("Medicine & Health",     "Nursing",                ("nursing", "midwifery")),
    ("Medicine & Health",     "Pharmacy",               ("pharmacy",)),
    ("Medicine & Health",     "Physiotherapy",          ("physiotherapy",)),
    ("Medicine & Health",     "Public Health",          ("public health",)),
    ("Medicine & Health",     "Psychology",             ("psychology",)),
    ("Medicine & Health",     "Dentistry",              ("dentistry", "dental")),
    ("Education & Social Work",   "Early Childhood",        ("early childhood",)),
    ("Education & Social Work",   "Social Work",            ("social work",)),
    ("Education & Social Work",   "Teaching",               ("teaching",)),
    ("Education & Social Work",   "TESOL",                  ("tesol",)),
    ("Architecture, Building & Design", "Architecture",     ("architecture",)),
    ("Architecture, Building & Design", "Interior Design",  ("interior design",)),
    ("Architecture, Building & Design", "Construction",     ("construction",)),
    ("Architecture, Building & Design", "Graphic Design",   ("graphic design",)),
    ("Media & Communications",    "Journalism",             ("journalism",)),
    ("Media & Communications",    "Public Relations",       ("public relations",)),
    ("Media & Communications",    "Film & Screen",          ("film", "screen")),
    ("Media & Communications",    "Digital Media",          ("digital media", "broadcasting")),
    ("Law & Legal Studies",       "Juris Doctor",           ("juris doctor", "jd ")),
    ("Law & Legal Studies",       "Criminal Justice",       ("criminal justice", "criminology")),
    ("Science & Mathematics",     "Biotechnology",          ("biotechnology", "genetics")),
    ("Science & Mathematics",     "Physics",                ("physics",)),
    ("Science & Mathematics",     "Chemistry",              ("chemistry", "biochemistry")),
    ("Science & Mathematics",     "Mathematics",            ("mathematics", "statistics")),
    ("Agriculture & Environmental Science", "Sustainability", ("sustainability",)),
    ("Agriculture & Environmental Science", "Agriculture",   ("agriculture", "agribusiness", "horticulture")),
    # Issue 2: Trades & Construction sub-categories for AQF vocational courses.
    ("Trades & Construction", "Carpentry",           ("carpentry",)),
    ("Trades & Construction", "Plumbing",            ("plumbing",)),
    ("Trades & Construction", "Electrical Trades",   ("electrical trade",)),
    ("Trades & Construction", "Welding & Fabrication", ("welding", "boilermaking", "fabrication")),
    ("Trades & Construction", "Cabinet Making",      ("cabinet making", "joinery")),
    ("Trades & Construction", "Refrigeration & HVAC", ("refrigeration", "air conditioning", "hvac")),
)


_PARENS_RE = re.compile(r"\(([^)]+)\)")


def map_course_to_category(course_name: str) -> dict | None:
    """Return ``{"category": str, "sub_category": str}`` if a confident
    keyword pre-map fires, otherwise ``None``.

    Two-pass strategy:
    1. Try matching ONLY the parenthetical portion of the course name
       (e.g. the "(Cyber Security)" in "Master of IT (Cyber Security)").
       Parentheticals are specialisation labels — they are a stronger
       sub-category signal than the prefix field name.
    2. Fall through to a full-name scan if the parenthetical yields nothing.

    Both passes use whole-word, case-insensitive, first-hit-wins matching
    against ``_SUB_CATEGORY_MAP``.
    """
    if not course_name:
        return None
    n = course_name.lower()

    def _match(text: str) -> dict | None:
        for category, sub_label, keywords in _SUB_CATEGORY_MAP:
            for kw in keywords:
                kw_clean = kw.strip()
                if not kw_clean:
                    continue
                if re.search(r"\b" + re.escape(kw_clean) + r"\b", text):
                    return {"category": category, "sub_category": sub_label}
        return None

    # Pass 1 — parenthetical content only
    parens = _PARENS_RE.findall(n)
    for paren_text in parens:
        hit = _match(paren_text)
        if hit:
            return hit

    # Pass 2 — full name
    return _match(n)


def classify_category(course_name: str) -> str | None:
    """Return the best-matching category or ``None`` when no rule fires.

    Caller decides whether to leave the field NULL or fall back to "Other".
    Returning ``None`` (rather than "Other") preserves the existing column
    semantics: a NULL means "we don't know", and the UI's review modal will
    flag it for the operator. "Other" implies "we looked and it doesn't fit".
    """
    scores = _score(course_name)
    if not scores:
        return None
    # Highest score wins. On ties, fall back to the order defined in
    # ``_KEYWORDS`` so output is deterministic — important so reviewers see
    # the same category for the same course on a re-scrape.
    best = max(scores.items(), key=lambda kv: (kv[1], -list(c for c, _ in _KEYWORDS).index(kv[0])))
    return best[0]
