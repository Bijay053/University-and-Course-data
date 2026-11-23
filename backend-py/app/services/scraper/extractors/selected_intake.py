"""Read a course's selected start-date variant, never the first dropdown item."""
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from .base import ExtractionResult

INTAKE_FACTS_VERSION = 3
MONTHS = ("January February March April May June July August September October November December").split()
DATE = re.compile(r"\b(" + "|".join(MONTHS) + r")\b", re.I)
LABEL = re.compile(r"(?:course\s+)?(?:start\s+dates?|intakes?)\s*:?", re.I)


def extract_selected_intake(html, url):
    soup = BeautifulSoup(html, "html.parser")
    canonicals = {
        urljoin(url, node["href"])
        for node in soup.select('head link[rel~="canonical"][href]')
    }
    candidates = []
    for select in soup.find_all("select"):
        if any(
            node.name in {"nav", "aside", "footer"} or
            re.search(r"related|recommended", " ".join(node.get("class", [])), re.I)
            for node in select.parents
        ):
            continue
        owner = None
        for parent in list(select.parents)[:4]:
            if parent.name in {"body", "html", "main"} or len(parent.get_text()) > 1800:
                break
            if len(parent.find_all("select")) == 1 and any(
                LABEL.fullmatch(tag.get_text(" ", strip=True))
                for tag in parent.find_all(["label", "dt", "p", "strong", "h3", "h4"])
            ):
                owner = parent
                break
        if owner is None:
            continue
        options = [o for o in select.find_all("option")
                   if DATE.search(o.get_text()) and not o.has_attr("disabled")
                   and not re.search(r"\bclosed\b|\bcancelled\b", o.get_text(), re.I)]
        if not options:
            continue
        linked = ("location" in select.get("onchange", "")
                  or any(re.match(r"https?://|/", o.get("value", "")) for o in options))
        if linked:
            # A URL-linked option is a separate offering, not an additional
            # intake to copy onto the current offering.
            def same_url(option):
                candidate = urlsplit(urljoin(url, option.get("value", "")))
                current = urlsplit(url)
                if (candidate.scheme, candidate.netloc, candidate.path.rstrip("/"), candidate.query) == (
                    current.scheme, current.netloc, current.path.rstrip("/"), current.query):
                    return True
                # Some official pages keep a dated alias while their selected
                # start option points at the undated canonical course page.
                # Require both the page's canonical declaration and its selected
                # month/year label; a URL suffix alone never supplies an intake.
                alias = re.fullmatch(
                    r"(.*)/(" + "|".join(MONTHS) + r")-(20\d{2})/?", current.path, re.I)
                return bool(
                    option.has_attr("selected") and alias and not current.query
                    and candidate.scheme == current.scheme and candidate.netloc == current.netloc
                    and candidate.path.rstrip("/") == alias[1].rstrip("/") and not candidate.query
                    and len(canonicals) == 1 and candidate.geturl() in canonicals
                    and re.fullmatch(
                        re.escape(alias[2]) + r"\s+" + re.escape(alias[3]),
                        option.get_text(" ", strip=True), re.I)
                )
            selected = [o for o in options if o.has_attr("selected")]
            chosen = selected if selected else [o for o in options if same_url(o)]
            if len(chosen) != 1 or not same_url(chosen[0]):
                continue
        else:
            # Non-URL choices are starts for the same course, not separate
            # course URLs. Preserve every explicitly listed month.
            chosen = options
        months = []
        for option in chosen:
            for match in DATE.finditer(option.get_text()):
                month = match[1].capitalize()
                if month not in months:
                    months.append(month)
        candidates.append(ExtractionResult(
            field_key="intake_months", value=months,
            normalized={"intake_months": months, "intake_days": None},
            confidence=1.0, snippet=owner.get_text(" ", strip=True),
            method="intake:selected_start_option",
            extras={"startOptions": [
                {"label": o.get_text(" ", strip=True),
                 "url": urljoin(url, o["value"]) if linked else None}
                for o in options
            ]},
        ))
    if len({tuple(c.value) for c in candidates}) != 1:
        return []
    return candidates[:1]
