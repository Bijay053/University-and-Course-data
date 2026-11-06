"""Remove explicitly irrelevant label/value pairs from one field's input only."""
import re
from bs4 import BeautifulSoup

DOMESTIC_FEE_LABEL = re.compile(
    r"^(?:domestic|home(?:/eu)?|uk|eu|eea|resident|in[- ]state|csp|"
    r"commonwealth supported|fee[- ]help)\b", re.I,
)
INTERNATIONAL = re.compile(r"\b(?:international|overseas|non[- ]?(?:uk|eu|eea|resident))\b", re.I)
DEADLINE_LABEL = re.compile(
    r"^(?:applications?\s+(?:close|closing|open|opening)|apply\s+by|"
    r"application\s+deadlines?|open\s+days?)\b", re.I,
)
START_LABEL = re.compile(r"\b(?:intakes?|start(?:ing)? dates?|commencement)\b", re.I)


def exclude_labelled_values(html, reject_label, preserve_value):
    """Never remove a sibling international amount or actual starting date.

    Horizontal table headers are left to the table parser. No ancestor sections
    are deleted; a label must own its immediately paired value.
    """
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for label in soup.find_all(["dt", "th", "strong", "b"]):
        if not label.parent or not reject_label.search(label.get_text(" ", strip=True)):
            continue
        if label.name in {"dt", "th"}:
            value = label.find_next_sibling()
            expected = "dd" if label.name == "dt" else "td"
            if not value or value.name != expected:
                continue
            if preserve_value.search(label.get_text(" ", strip=True) + " " + value.get_text(" ", strip=True)):
                continue
            value.decompose()
            label.decompose()
            changed = True
        elif label.parent.name == "p":
            parent = label.parent
            if not preserve_value.search(parent.get_text(" ", strip=True)):
                parent.decompose()
                changed = True
    return str(soup) if changed else html
