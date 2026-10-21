"""Keep explicitly labelled academic-level IELTS table cells separate."""
import re

from bs4 import BeautifulSoup


def named_ielts_cells(html: str) -> list[tuple[str, str]]:
    """Read only explicit course/IELTS columns, not general qualifications."""
    result = []
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        rows = [
            [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
            for row in table.find_all("tr") if row.find_parent("table") is table
        ]
        if not rows or len(rows[0]) != 2:
            continue
        if rows[0][0].casefold() != "course" or not re.fullmatch(
            r"(?:specific\s+)?IELTS\s+requirements?", rows[0][1], re.I,
        ):
            continue
        result.extend((row[0], row[1]) for row in rows[1:] if len(row) == 2 and row[0])
    return result


def level_ielts_cells(html: str) -> dict[str, str] | None:
    return _level_cells(html, r"IELTS(?:\s*(?:Academic|\(Academic\)))?")


def other_level_scores(html: str) -> dict[str, dict[str, float]]:
    """Retain other tests in their own level columns and native score scales."""
    from .extractors import english_test

    output: dict[str, dict[str, float]] = {}
    specs = (
        ("pte", r"(?:Pearson(?:\s+PTE(?:\s+Academic)?)?|PTE(?:\s+Academic)?)", "PTE Academic", english_test._pte),
        ("toefl", r"TOEFL(?:\s+iBT)?", "TOEFL iBT", english_test._toefl),
        ("cambridge", r"Cambridge English Examinations", "Cambridge English", english_test._cambridge),
        ("duolingo", r"Duolingo(?:\s+English\s+Test)?", "Duolingo", english_test._duolingo),
    )
    for key, label, prefix, parser in specs:
        for level, cell in (_level_cells(html, label) or {}).items():
            parsed = parser(f"{prefix} {cell}")
            if parsed is None:
                continue
            values = parsed if isinstance(parsed, dict) else {"overall": parsed}
            # The new 1–6 TOEFL scale must not be stored as a legacy 0–120
            # score. A conversion requires an explicit, supported scale.
            if key == "toefl" and values.get("overall", 0) <= 6:
                continue
            output.setdefault(level, {}).update({
                f"{key}_{slot}": score for slot, score in values.items()
                if score is not None
            })
    return output


def _level_cells(html: str, label_pattern: str) -> dict[str, str] | None:
    candidates: dict[str, set[str]] = {}
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        rows = [
            [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
            for row in table.find_all("tr")
            if row.find_parent("table") is table
        ]
        if not rows:
            continue
        headers = rows[0]
        columns = {}
        for level in ("undergraduate", "postgraduate"):
            positions = [i for i, label in enumerate(headers) if label.casefold() == level]
            if len(positions) == 1:
                columns[level] = positions[0]
        if len(columns) != 2:
            continue
        for row in rows[1:]:
            if len(row) != len(headers) or not re.fullmatch(
                label_pattern, row[0], re.I,
            ):
                continue
            for level, index in columns.items():
                candidates.setdefault(level, set()).add(row[index])
    # Conflicting published rows cannot be resolved by choosing the first.
    if not candidates:
        return None
    return {
        level: next(iter(values))
        for level, values in candidates.items() if len(values) == 1
    }
