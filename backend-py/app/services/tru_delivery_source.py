"""Resolve TRU's selected programme label without reading its related catalogue."""
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def materialize_delivery(html, url):
    selected = urlsplit(url)
    if selected.hostname not in {"tru.ca", "www.tru.ca"} or selected.query:
        return html
    if not selected.path.startswith(("/distance/programs/", "/programs/catalogue/")):
        return html
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for script in soup.find_all("script"):
        text = script.get_text()
        for match in re.finditer(r"\bvar\s+arrCurrentProgram\s*=\s*", text):
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            except ValueError:
                return html
            records.append(value)
    if len(records) != 1 or not isinstance(records[0], dict):
        return html
    record = records[0]
    own = urlsplit(str(record.get("url") or ""))
    path = lambda p: re.sub(r"\.html$", "", p.rstrip("/"))
    if own.hostname not in {"tru.ca", "www.tru.ca"} or own.query or path(own.path) != path(selected.path):
        return html
    key = lambda s: " ".join(str(s).casefold().split())
    heads = [h for h in soup.select("main h1,article h1") if h.get_text(strip=True)]
    if len(heads) != 1 or key(heads[0].get_text(" ", strip=True)) != key(record.get("credentialName")):
        return html
    boxes = soup.select("main .info-delivery,article .info-delivery")
    if len(boxes) != 1:
        return html
    box = boxes[0]
    label, value = box.select_one(".info-title"), box.select_one(".info-text")
    if not label or not value or key(label.get_text()) != "delivery":
        return html
    mode = {"distance": "Online", "online": "Online", "campus": "On Campus"}.get(key(record.get("delivery")))
    if not mode or (value.get_text(strip=True) and key(value.get_text()) != key(mode)):
        return html
    # Materialize only the official selected label. Retain all other page content
    # so campus attendance checks can still veto an online-only conclusion.
    value.string = mode
    box.name = "p"
    return str(soup)
