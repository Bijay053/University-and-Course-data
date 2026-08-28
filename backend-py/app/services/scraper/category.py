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

from app.services.scraper.taxonomy import (
    CATEGORIES,
    LEGACY_PARENT_ALIASES,
    canonical_parent,
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
            "biomedical science", "midwifery", "paramedic",
            # Note: bare "psychology" lives under Arts, Humanities & Social
            # Sciences (matches the live DB taxonomy). Only the clinical
            # variant belongs to Medicine & Health — caught by the multi-word
            # phrase below.
            "clinical psychology", "clinical",
            "radiography", "medical", "healthcare", "podiatry",
            "optometry", "chiropractic", "veterinary",
        ),
    ),
    (
        "Arts, Humanities & Social Sciences",
        (
            "arts", "humanities", "history", "philosophy", "sociology",
            "anthropology", "linguistics", "literature", "religion",
            "political science", "international relations", "criminology",
            "social science", "language", "translation", "interpreting",
            "creative writing", "music",
            "performing arts", "theatre", "fine arts",
            # Psychology lives here per the live DB taxonomy
            # ("course_sub_categories" has Psychology under
            # Arts, Humanities & Social Sciences, and Clinical Psychology
            # under Medicine & Health).
            "psychology",
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
    # Cross-category precedence: this specific media phrase must beat the
    # generic Business & Management "marketing" rule below.
    ("Media & Communications",    "Media Marketing",        ("media marketing", "marketing communication", "marketing communications")),
    ("Business & Management",     "MBA",                    ("mba", "master of business administration")),
    ("Business & Management",     "Business Administration", ("business administration",)),
    ("Business & Management",     "Accounting",             ("accounting", "accountancy")),
    ("Business & Management",     "Actuarial Science",      ("actuarial science", "actuarial studies")),
    ("Business & Management",     "Finance",                ("finance", "banking", "actuarial")),
    ("Business & Management",     "Commerce",               ("commerce",)),
    ("Business & Management",     "Economics",              ("economics",)),
    ("Business & Management",     "Marketing",              ("marketing",)),
    ("Business & Management",     "Project Management",     ("project management",)),
    ("Business & Management",     "International Business", ("international business",)),
    ("Business & Management",     "Supply Chain & Logistics", ("supply chain", "logistics")),
    ("Business & Management",     "Human Resources",        ("human resource", "hr management")),
    ("Business & Management",     "Technology Management",  ("technology management",)),
    ("Business & Management",     "Entrepreneurship",       ("entrepreneurship",)),
    # Business Analytics must come before bare "Management" to avoid
    # "Business Analytics" being swallowed by the "management" keyword.
    ("Business & Management",     "Business Analytics",     ("business analytics",)),
    # General management — catches "Bachelor of Business Management",
    # "Master of Management", "Graduate Diploma of Business Management" etc.
    # Listed after more-specific sub-categories so specific terms win first.
    ("Business & Management",     "Management",             ("business management", "management studies", "master of management", "graduate diploma of management", "graduate certificate of management")),
    ("Computer Science & IT",     "Networking",             ("networking", "network engineering", "computer networks")),
    ("Computer Science & IT",     "Data Science",           ("data science", "data analytics")),
    # "cyber" alone matches "cyber security"; "cybersecurity" handles the
    # one-word spelling that the word-boundary regex would otherwise miss.
    ("Computer Science & IT",     "Cyber Security",         ("cyber", "cybersecurity")),
    ("Computer Science & IT",     "Artificial Intelligence", ("artificial intelligence", "machine learning")),
    ("Computer Science & IT",     "Software Engineering",   ("software engineering", "software development", "software application development", "application development")),
    ("Computer Science & IT",     "Information Systems",    ("information systems", "information technology", "it management")),
    ("Computer Science & IT",     "Computer Science",       ("computer science", "computing science", "applied computing", "bsc computing", "master of computing", "graduate diploma in computing", "graduate certificate in computing")),
    ("Engineering & Technology",  "Mechatronics",           ("mechatronic", "mechatronics")),
    ("Engineering & Technology",  "Robotics",               ("robotics",)),
    ("Engineering & Technology",  "Automotive Engineering", ("automotive engineering", "motorsport engineering", "motorsports engineering")),
    ("Engineering & Technology",  "Engineering Management", ("engineering management",)),
    ("Engineering & Technology",  "Mechanical Engineering", ("mechanical engineering",)),
    ("Engineering & Technology",  "Civil Engineering",      ("civil engineering",)),
    ("Engineering & Technology",  "Electrical Engineering", ("electrical engineering", "electronic engineering", "electrical and electronic engineering", "electrical & electronic engineering")),
    ("Engineering & Technology",  "Biomedical Engineering", ("biomedical engineering",)),
    ("Engineering & Technology",  "Chemical Engineering",   ("chemical engineering",)),
    ("Engineering & Technology",  "General Engineering & Technology", ("engineering technology", "general engineering", "bachelor of engineering", "master of engineering", "associate degree of engineering", "engineering (honours)")),
    ("Medicine & Health",     "Nursing",                ("nursing", "midwifery")),
    ("Medicine & Health",     "Pharmacy",               ("pharmacy",)),
    ("Medicine & Health",     "Physiotherapy",          ("physiotherapy",)),
    ("Medicine & Health",     "Public Health",          ("public health",)),
    ("Medicine & Health",     "Health Sciences",        ("digital health", "health sciences", "health science")),
    ("Medicine & Health",     "Biomedical Sciences",    ("medical sciences", "medical science", "biomedical sciences")),
    ("Medicine & Health",     "Veterinary Medicine",    ("veterinary medicine", "veterinary science", "veterinary technology")),
    ("Medicine & Health",     "Nutrition & Dietetics",  ("clinical nutrition", "nutrition", "dietetics")),
    # Clinical Psychology stays in Medicine & Health (matches DB taxonomy).
    # Plain "psychology" is mapped to the Arts/Humanities bucket below.
    ("Medicine & Health",     "Clinical Psychology",    ("clinical psychology",)),
    ("Arts, Humanities & Social Sciences", "Psychology", ("psychology",)),
    ("Medicine & Health",     "Dentistry",              ("dentistry", "dental")),
    ("Medicine & Health",     "Human Medicine",         ("doctor of medicine", "bachelor of medicine", "medicine and surgery")),
    ("Education & Social Work",   "Early Childhood",        ("early childhood",)),
    ("Education & Social Work",   "Social Work",            ("social work",)),
    ("Education & Social Work",   "Teaching",               ("teaching",)),
    ("Education & Social Work",   "TESOL",                  ("tesol",)),
    ("Architecture, Building & Design", "Architecture",     ("architecture",)),
    ("Architecture, Building & Design", "Interior Design",  ("interior design",)),
    ("Architecture, Building & Design", "Construction",     ("construction",)),
    ("Architecture, Building & Design", "Graphic Design",   ("graphic design",)),
    ("Architecture, Building & Design", "Fashion Design",   ("fashion design",)),
    ("Architecture, Building & Design", "Industrial Design", ("industrial design", "product design")),
    ("Architecture, Building & Design", "User Experience Design", ("user experience", "ux design")),
    ("Architecture, Building & Design", "Design",           ("design studies", "design innovation", "bachelor of design", "master of design", "art and design", "art & design")),
    ("Media & Communications",    "Journalism",             ("journalism",)),
    ("Media & Communications",    "Public Relations",       ("public relations",)),
    ("Media & Communications",    "Film & Screen",          ("film", "screen")),
    ("Media & Communications",    "Digital Media",          ("digital media", "broadcasting")),
    ("Media & Communications",    "Film Photography & Media", ("animation", "media production", "creative media", "television production", "visual effects")),
    ("Media & Communications",    "Media Studies & Mass Media", ("publishing",)),
    ("Media & Communications",    "Media Management",       ("media management",)),
    # General media/communication titles must come after every more-specific
    # media rule.  The canonical DB taxonomy calls this discipline
    # "Communications"; without this fallback titles such as "Bachelor of Media
    # and Communication" received the parent category but a blank sub-category.
    ("Media & Communications",    "Communications",          ("communication", "communications", "media studies", "mass media")),
    ("Law & Legal Studies",       "Juris Doctor",           ("juris doctor", "jd ")),
    ("Law & Legal Studies",       "Criminal Justice",       ("criminal justice", "criminology")),
    ("Science & Mathematics",     "Biotechnology",          ("biotechnology", "genetics")),
    ("Science & Mathematics",     "Physics",                ("physics",)),
    ("Science & Mathematics",     "Chemistry",              ("chemistry", "biochemistry")),
    ("Science & Mathematics",     "Mathematics",            ("mathematics", "statistics")),
    ("Agriculture & Environmental Science", "Sustainability", ("sustainability", "sustainable environment")),
    ("Agriculture & Environmental Science", "Agriculture",   ("agriculture", "agribusiness", "horticulture")),
    ("Agriculture & Environmental Science", "Environmental Science", ("environmental science", "environmental management", "environmental studies", "ecology", "conservation", "wildlife management", "forestry", "environmental protection")),
    ("Agriculture & Environmental Science", "Marine Science", ("marine science", "marine biology", "ocean", "aquaculture")),
    # Issue 2: Trades & Construction sub-categories for AQF vocational courses.
    ("Trades & Construction", "Carpentry",           ("carpentry",)),
    ("Trades & Construction", "Plumbing",            ("plumbing",)),
    ("Trades & Construction", "Electrical Trades",   ("electrical trade",)),
    ("Trades & Construction", "Welding & Fabrication", ("welding", "boilermaking", "fabrication")),
    ("Trades & Construction", "Cabinet Making",      ("cabinet making", "joinery")),
    ("Trades & Construction", "Refrigeration & HVAC", ("refrigeration", "air conditioning", "hvac")),
    # ── Arts, Humanities & Social Sciences sub-categories ───────────────────
    # Multi-word / more-specific phrases listed before bare single-word terms.
    ("Arts, Humanities & Social Sciences", "International Relations", ("international relations", "global studies", "international studies", "diplomacy")),
    ("Arts, Humanities & Social Sciences", "Political Science",       ("political science", "politics", "public policy", "public administration", "governance")),
    ("Arts, Humanities & Social Sciences", "Sociology",               ("sociology", "social science", "social sciences", "criminology")),
    ("Arts, Humanities & Social Sciences", "Anthropology",            ("anthropology",)),
    ("Arts, Humanities & Social Sciences", "Philosophy",              ("philosophy",)),
    ("Arts, Humanities & Social Sciences", "History",                 ("history", "historical",)),
    ("Arts, Humanities & Social Sciences", "Linguistics",             ("translation studies", "translation", "translating", "interpreting", "linguistics")),
    ("Arts, Humanities & Social Sciences", "English & Literature",    ("english literature", "creative writing", "writing", "english language")),
    ("Arts, Humanities & Social Sciences", "Geography",               ("geography", "urban planning", "regional planning")),
    ("Arts, Humanities & Social Sciences", "Arts",                    ("bachelor of arts", "master of arts", "liberal arts")),
    # ── Medicine & Health additional sub-categories ──────────────────────────
    ("Medicine & Health", "Occupational Therapy",  ("occupational therapy",)),
    ("Medicine & Health", "Speech Pathology",      ("speech pathology", "speech language", "audiology")),
    ("Medicine & Health", "Paramedicine",          ("paramedicine", "paramedic", "paramedics")),
    ("Medicine & Health", "Optometry",             ("optometry", "optics", "optical")),
    ("Medicine & Health", "Health Management",     ("health management", "health administration", "health services management", "healthcare management")),
    ("Medicine & Health", "Radiography",           ("radiography", "medical imaging", "diagnostic imaging")),
    ("Medicine & Health", "Medical Laboratory",    ("medical laboratory", "medical science", "pathology science", "biomedical science")),
    ("Medicine & Health", "Podiatry",              ("podiatry",)),
    ("Medicine & Health", "Midwifery",             ("midwifery",)),
    # ── Engineering & Technology additional sub-categories ───────────────────
    ("Engineering & Technology", "Aerospace Engineering",     ("aerospace", "aeronautical", "aviation engineering")),
    ("Engineering & Technology", "Environmental Engineering", ("environmental engineering",)),
    ("Engineering & Technology", "Mining Engineering",        ("mining engineering", "resources engineering")),
    ("Engineering & Technology", "Construction Management",   ("construction management",)),
    ("Engineering & Technology", "Telecommunications",        ("telecommunications", "telecom",)),
    # ── Business & Management general catchall ───────────────────────────────
    # Listed last in its category so more-specific entries win first.
    ("Business & Management", "Business",           ("bachelor of business", "master of business", "graduate certificate of business", "graduate diploma of business", "graduate certificate in business", "graduate diploma in business", "diploma of business", "diploma in business")),
    # ── Law & Legal Studies sub-categories ───────────────────────────────────
    ("Law & Legal Studies", "Laws",                 ("bachelor of laws", "master of laws", "juris doctor", "llb", "llm", "law phd")),
    ("Law & Legal Studies", "Legal Practice",       ("legal practice", "legal studies", "paralegal")),
    # ── Science & Mathematics additional sub-categories ──────────────────────
    ("Science & Mathematics", "Biology",            ("biology", "biological sciences", "microbiology", "molecular biology", "genetics")),
    ("Science & Mathematics", "Earth Sciences",     ("geology", "earth science", "geoscience", "geophysics")),
    ("Science & Mathematics", "Psychology",         ("applied psychology",)),  # clinical is already in Medicine & Health
    # ── Computer Science & IT general catchall ───────────────────────────────
    ("Computer Science & IT", "Information Technology", ("bachelor of information technology", "master of information technology", "graduate certificate of information technology")),
    # ── Education sub-categories ─────────────────────────────────────────────
    ("Education & Social Work", "Educational Leadership", ("educational leadership", "school leadership", "principal",)),
    ("Education & Social Work", "Special Education",      ("special education", "inclusive education", "disability studies")),
    ("Education & Social Work", "Counselling",            ("counselling", "counseling", "guidance counselling")),
    ("Education & Social Work", "Education",               ("bachelor of education", "master of education", "graduate certificate in education", "graduate diploma in education")),
    # A bare hospitality title still belongs to the canonical Hospitality
    # Management branch; specific tourism/events/cookery rules appear first.
    ("Hospitality, Tourism & Events", "Hospitality Management", ("bachelor of hospitality", "master of hospitality", "hospitality studies")),
)


_PARENS_RE = re.compile(r"\(([^)]+)\)")


_GENERIC_DOCTORATE_RE = re.compile(
    r"^\s*(doctor of philosophy|doctor of professional studies|master of philosophy|"
    r"phd|d\.phil|dphil|m\.phil|mphil)\s*$",
    re.IGNORECASE,
)


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

    Generic doctorate guard: bare "Doctor of Philosophy" / "PhD" / "MPhil"
    have no field info — return None so the reviewer picks the discipline
    rather than letting downstream defaults bucket them as "Maths & Sciences".
    """
    if not course_name:
        return None
    if _GENERIC_DOCTORATE_RE.match(course_name.strip()):
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


def _category_key(value: str | None) -> str:
    """Return a comparison key for parent-category labels.

    Extractors sometimes use ``and`` while the controlled taxonomy uses ``&``.
    This helper is deliberately comparison-only: it never rewrites a stored
    category or turns a non-canonical source label into a new taxonomy value.
    """
    if not value:
        return ""
    key = re.sub(r"[^a-z0-9]+", " ", value.lower().replace("&", " and ")).strip()
    aliases = {
        "engineering": "engineering and technology",
        "health sciences": "medicine and health",
        "education": "education and social work",
        "science": "science and mathematics",
        "arts and humanities": "arts humanities and social sciences",
        "it and computer science": "computer science and it",
    }
    aliases.update(
        {
            re.sub(
                r"[^a-z0-9]+", " ", legacy.lower().replace("&", " and ")
            ).strip(): re.sub(
                r"[^a-z0-9]+", " ", canonical.lower().replace("&", " and ")
            ).strip()
            for legacy, canonical in LEGACY_PARENT_ALIASES.items()
        }
    )
    return aliases.get(key, key)


def infer_course_taxonomy(
    course_name: str,
    category: str | None = None,
    sub_category: str | None = None,
) -> dict[str, str | None]:
    """Fill missing taxonomy fields from the shared deterministic mapper.

    Non-blank caller values always win.  A deterministic sub-category is used
    only when its parent agrees with the existing/inferred parent category, so
    a broad title keyword cannot silently cross category boundaries.

    This is the single pure helper used by extraction, staging, approval, and
    the historical backfill.  Keeping the decision here prevents those paths
    from drifting and makes the backfill idempotent.
    """
    clean_category = canonical_parent(category)
    clean_sub = sub_category.strip() if sub_category and sub_category.strip() else None

    # A bare research-degree title contains no discipline signal.  Keep any
    # real caller-supplied taxonomy, but never let the broad classifier turn
    # "Doctor of Philosophy" / "MPhil" into a guessed parent category.
    if course_name and _GENERIC_DOCTORATE_RE.match(course_name.strip()):
        return {
            "category": clean_category,
            "sub_category": clean_sub,
        }

    deterministic = map_course_to_category(course_name)
    resolved_category = clean_category
    if not resolved_category:
        resolved_category = (
            deterministic.get("category")
            if deterministic
            else classify_category(course_name)
        )

    resolved_sub = clean_sub
    if (
        not resolved_sub
        and deterministic
        and _category_key(resolved_category) == _category_key(deterministic.get("category"))
    ):
        resolved_sub = deterministic.get("sub_category")

    return {
        "category": resolved_category,
        "sub_category": resolved_sub,
    }


def subcategory_options_for_category(category: str | None) -> tuple[str, ...]:
    """Return deterministic controlled-vocabulary options for one parent."""
    key = _category_key(category)
    if not key:
        return ()
    options: list[str] = []
    for parent, sub_category, _keywords in _SUB_CATEGORY_MAP:
        if _category_key(parent) == key and sub_category not in options:
            options.append(sub_category)
    return tuple(options)
