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
