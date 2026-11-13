"""Course-owned and filtered-catalogue evidence; never infer a school from a title."""
import asyncio
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def normal(value):
    return " ".join(str(value or "").split())


def key(value):
    return re.sub(r"^(?:school|college|faculty)\s+of\s+", "", normal(value).casefold()).replace("&", "and")


def level(value):
    value = normal(value)
    if re.search(r"undergraduate|bachelor", value, re.I):
        return "undergraduate"
    if re.search(r"research|doctoral|ph\.?d", value, re.I):
        return "postgraduate_research"
    if re.search(r"postgraduate|masters?|MA/MSc", value, re.I):
        return "postgraduate_taught"


def owned_url(value, base):
    target = urlsplit(urljoin(base, value))
    origin = urlsplit(base)
    if target.scheme != "https" or target.netloc != origin.netloc or target.username or target.password:
        return None
    return urlunsplit((target.scheme, target.netloc, target.path.rstrip("/"), target.query, ""))


def metadata(html, url):
    """Keep public GET filter controls, not form tokens, navigation or staff input."""
    soup = BeautifulSoup(html, "html.parser")
    filters, selected, records, schools, school_links, variants = [], [], [], [], [], []
    selected_controls = {}
    for anchor in soup.select("a[href]"):
        label = normal(anchor.get_text(" ", strip=True))
        target = owned_url(anchor["href"], url)
        if target and re.search(r"\b(?:school|college|faculty|sciences)\b", label, re.I) and len(label) <= 120:
            school_links.append({"label": label, "url": target})
    scope_labels = [normal(n.get_text(" ", strip=True)) for n in soup.select("h1,.breadcrumb li")]
    if soup.title:
        scope_labels.append(normal(soup.title.get_text(" ", strip=True)).split("|", 1)[0].strip())
    for selector in soup.select("select.other_variant_switch,select[data-course-variants]"):
        options = selector.select("option[value]")
        if options and all(re.fullmatch(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}",
            normal(o.get_text(" ", strip=True)), re.I) for o in options):
            urls = [owned_url(o["value"], url) for o in options]
            if owned_url(url, url) in urls and all(urls):
                variants.extend({"url": u, "quote": normal(selector.get_text(" ", strip=True))} for u in urls)
    breadcrumb = soup.select_one(".breadcrumb")
    last = breadcrumb.select("li")[-1] if breadcrumb and breadcrumb.select("li") else None
    title = normal(last.get_text(" ", strip=True)) if last else ""
    next_link = next((a for a in soup.select("a[href]") if re.search(
        r"^Next page", a.get_text(" ", strip=True), re.I)), None)
    next_url = owned_url(next_link["href"], url) if next_link else None
    for form in soup.find_all("form"):
        if str(form.get("method", "get")).lower() != "get":
            continue
        action = owned_url(form.get("action", url), url)
        if not action:
            continue
        for control in form.select('input[type="checkbox"][name][value]'):
            name, value = control["name"], control["value"]
            label = control.find_parent("label")
            label = label or soup.find("label", attrs={"for": control.get("id", "")})
            if not label or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*(?:\[\d+\])?", name):
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", value):
                continue
            text = normal(label.get_text(" ", strip=True))
            target = urlsplit(action)
            query = parse_qs(target.query)
            query[name] = [value]
            endpoint = urlunsplit((*target[:3], urlencode(query, doseq=True), ""))
            filters.append({"label": text, "url": endpoint, "parameter": name, "value": value})
            if control.has_attr("checked") and parse_qs(urlsplit(url).query).get(name) == [value]:
                selected.append(text)
                selected_controls[name] = [value]
    for node in soup.select("script,style,nav,footer,header,aside,form,[class*='related']"):
        node.decompose()
    for heading in soup.select(".course-title,.programme-title"):
        anchor = heading.find_parent("a", href=True)
        if not anchor:
            continue
        target = owned_url(anchor["href"], url)
        qualification = anchor.select_one(".program-type,.programme-type,.course-level")
        scope = level(qualification.get_text(" ", strip=True)) if qualification else None
        if target and scope:
            records.append({"url": target, "title": normal(heading.get_text(" ", strip=True)),
                            "level": scope, "quote": normal(heading.get_text(" ", strip=True))
                            + " " + normal(qualification.get_text(" ", strip=True))})
    for label in soup.select("dt,.field--label,th"):
        if not re.fullmatch(r"(?:school|college|faculty)\s*:?", normal(label.get_text(" ", strip=True)), re.I):
            continue
        value = label.find_next_sibling(["dd", "td"]) if label.name in {"dt", "th"} else label.find_next_sibling()
        if value and len(normal(value.get_text(" ", strip=True))) <= 120:
            schools.append({"label": normal(value.get_text(" ", strip=True)),
                            "quote": normal(label.get_text(" ", strip=True)) + " "
                            + normal(value.get_text(" ", strip=True))})
    # Breadcrumbs are identity evidence, not evidence of school membership.
    catalogue_links = []
    linked_courses = []
    for anchor in soup.select("a[href]"):
        target = owned_url(anchor["href"], url)
        heading = anchor.find(["h2", "h3", "h4", "h5"])
        label = normal((heading or anchor).get_text(" ", strip=True))
        if target and (level(label) or re.fullmatch(r"courses|programmes|programs|study(?: with us)?", label, re.I)):
            catalogue_links.append({"url": target, "label": label})
        if target and len(label) <= 200 and re.search(r"/(?:courses|programmes|programs|degrees)/", urlsplit(target).path):
            linked_courses.append({"url": target, "title": label, "quote": label, "level": None})
    return {"schema": 2, "filters": filters[:50], "selected": selected, "selectedControls": selected_controls, "records": records,
            "schools": schools, "identity": title if len(title) <= 200 else "",
            "schoolLinks": list({p["url"]: p for p in school_links}.values())[:50],
            "scopeLabels": scope_labels, "catalogueLinks": catalogue_links[:20], "next": next_url,
            "linkedCourses": linked_courses, "variants": variants}


async def enrich(course, guidance, home, reader, validator, cache):
    """Read each relevant filtered catalogue once, with at most eight pages."""
    from app.services.document_course_source import read_course
    from app.services.document_ielts_matching import identity_matches
    evidence = list(course.get("cohortEvidence", []))
    data = course.get("_courseCohorts") or {}
    labels = {key(p["label"]) for p in guidance.get("_englishTables", [])}
    for school in data.get("schools", []):
        if key(school["label"]) in labels and school["quote"] in normal(course.get("_fullText")):
            evidence.append({**school, "url": course["url"], "level": None})

    async def cached_reader(url, home):
        sources = cache.setdefault("__sources", {})
        if url not in sources:
            if len(sources) >= 96:
                return {"verified": False, "error": "Official cohort evidence page limit reached."}
            sources[url] = asyncio.create_task(reader(url, home))
        return await sources[url]

    async def permitted_page(url):
        return await read_course(url, home, cached_reader, validator)

    async def catalogue(filter_):
        current, found, visited = filter_["url"], [], set()
        requirements = filter_.get("requirements") or {filter_["parameter"]: [filter_["value"]]}
        for _ in range(8):
            if current in visited:
                break
            visited.add(current)
            parsed = parse_qs(urlsplit(current).query)
            if any(parsed.get(k) != v for k, v in requirements.items()):
                break
            page = await permitted_page(current)
            meta = page.get("_courseCohorts") or {}
            if not page.get("verified") or any(meta.get("selectedControls", {}).get(k) != v for k, v in requirements.items()):
                break
            for record in meta.get("records", []):
                if record["quote"] in normal(page.get("_fullText")):
                    found.append({**record, "label": filter_["label"], "sourceUrl": page["url"],
                                  "scopeSource": filter_.get("scopeSource")})
            current = meta.get("next")
            if not current:
                break
        return found

    async def school_catalogue(link):
        pending, visited, found = [(link["url"], None)], set(), []
        for _ in range(5):
            if not pending:
                break
            target, inherited_level = pending.pop(0)
            if target in visited:
                break
            visited.add(target)
            page = await permitted_page(target)
            meta = page.get("_courseCohorts") or {}
            scoped = next((s for s in meta.get("scopeLabels", [])
                           if key(re.sub(r"^courses\s*\((.*)\)$", r"\1", s, flags=re.I))
                           == key(link["label"])), None)
            if not page.get("verified") or not scoped or scoped not in normal(page.get("_fullText")):
                break
            visited.add(page["url"])
            if inherited_level and any(level(s) == inherited_level for s in meta.get("scopeLabels", [])):
                for record in meta.get("linkedCourses", []):
                    if record["quote"] in normal(page.get("_fullText")):
                        found.append({**record, "level": inherited_level, "label": link["label"],
                                      "sourceUrl": page["url"], "scopeSource": None})
            for child in meta.get("catalogueLinks", []):
                child_url = validator(child["url"], home)
                parameters = {k: v for k, v in parse_qs(urlsplit(child_url).query).items()
                              if re.fullmatch(r"[a-zA-Z_]\w*\[\d+\]", k)}
                if parameters and level(child["label"]) in {"undergraduate", "postgraduate_taught"}:
                    descriptor = {"url": child_url, "label": link["label"], "requirements": parameters,
                                  "scopeSource": {"url": page["url"], "quote": scoped}}
                    if child_url not in cache:
                        cache[child_url] = asyncio.create_task(catalogue(descriptor))
                    found.extend(await cache[child_url])
                elif re.fullmatch(r"courses|programmes|programs|study(?: with us)?", child["label"], re.I):
                    if child_url not in visited and child_url not in {u for u, _ in pending}:
                        pending.append((child_url, None))
                elif (level(child["label"]) in {"undergraduate", "postgraduate_taught"}
                      and not urlsplit(child_url).path.lower().endswith(".pdf")):
                    if child_url not in visited and child_url not in {u for u, _ in pending}:
                        pending.append((child_url, level(child["label"])))
        return found

    collections = []
    for filter_ in data.get("filters", []):
        if key(filter_["label"]) not in labels:
            continue
        target = validator(filter_["url"], home)
        if target not in cache:
            cache[target] = asyncio.create_task(catalogue(filter_))
        try:
            records = await cache[target]
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
        collections.extend(records)
    for link in data.get("schoolLinks", []):
        if key(link["label"]) not in labels:
            continue
        target = validator(link["url"], home)
        cache_key = "school:" + target
        if cache_key not in cache:
            cache[cache_key] = asyncio.create_task(school_catalogue(link))
        try:
            collections.extend(await cache[cache_key])
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    urls = {owned_url(course.get(k) or "", course["url"]) for k in ("url", "requestedUrl")}
    direct_urls = set(urls)
    variant_aliases = {v["url"]: v for v in data.get("variants", [])}
    urls.update(variant_aliases)
    def intake_identity(record):
        return bool(data.get("variants")
                    and re.search(r"\b(?:BA|BSc|BEng|BAcc|BBA|LLB|MA|MSc|MEng|MBA|MPharm|PGDip|PGCert)\b",
                                  course.get("courseTitle", ""), re.I)
                    and normal(record["title"]).lower() == normal(course.get("courseTitle")).lower())
    # Some universities advertise the default intake with an unversioned URL,
    # while their catalogue links to its final, year-stamped URL.
    for variant in ([] if any(intake_identity(r) for r in collections) else data.get("variants", [])):
        if variant["url"] not in direct_urls and not re.search(
                r"/(?:january|february|march|april|may|june|july|august|september|october|november|december)-20\d{2}/?$",
                urlsplit(variant["url"]).path, re.I):
            resolved = await permitted_page(variant["url"])
            if (resolved.get("verified") and resolved.get("courseTitle")
                    and identity_matches(course["courseTitle"], resolved["courseTitle"])
                    and normal(resolved["courseTitle"]) in normal(resolved.get("_fullText"))):
                urls.add(owned_url(resolved["url"], course["url"]))
                variant_aliases[owned_url(resolved["url"], course["url"])] = variant
    for record in collections:
        if (record["url"] in urls or intake_identity(record)) and identity_matches(course.get("courseTitle", ""), record["title"]):
            evidence.append({"label": record["label"], "url": record["sourceUrl"],
                             "level": record["level"], "quote": record["quote"],
                             "courseUrl": record["url"]})
            if record.get("scopeSource"):
                evidence.append({**record["scopeSource"], "label": record["label"], "level": record["level"]})
            if record["url"] not in {owned_url(course.get(k) or "", course["url"]) for k in ("url", "requestedUrl")}:
                variant = variant_aliases.get(record["url"]) or data["variants"][0]
                evidence.append({"url": course["url"], "quote": variant["quote"],
                                 "label": record["label"], "level": record["level"]})
    return {**course, "cohortEvidence": evidence}
