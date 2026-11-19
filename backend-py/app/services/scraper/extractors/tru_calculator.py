"""TRU's course-bound calculator: four courses, international tuition only."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from .base import ExtractionResult

INIT = "/__shared/templates/2019/js/programs-2020-cost-calculator-init.js"
_ASSET_READS = ContextVar("tru_calculator_asset_reads", default=None)


@asynccontextmanager
async def asset_session():
    """Share public assets only within this one bounded preview, never globally."""
    reads = {}
    token = _ASSET_READS.set(reads)
    try:
        yield
    finally:
        for task in reads.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*reads.values(), return_exceptions=True)
        _ASSET_READS.reset(token)


def assets(html, url):
    if urlsplit(url).hostname not in {"tru.ca", "www.tru.ca"}:
        return None
    if not urlsplit(url).path.startswith("/programs/catalogue/"):
        return None
    soup = BeautifulSoup(html, "html.parser")
    fees = soup.select_one(".fees #tuition-table")
    widget = fees.find_parent(class_="fees") if fees else None
    if not widget:
        return None
    text = widget.get_text(" ", strip=True)
    chooser = widget.select_one('input[name="tuition-fees-range"]')
    if (not re.search(r"Cost for this program per semester", text, re.I)
            or not widget.select_one('[data-id="international"]')
            or not chooser or chooser.get("value") != "4"):
        return ()
    scripts = [urljoin(url, s["src"]) for s in soup.select("script[src]")]
    root = f"https://{urlsplit(url).hostname}"
    data = list(dict.fromkeys(s for s in scripts if re.fullmatch(
        re.escape(root) + r"/__shared/assets/academic\d+\.js", s)))
    if root + INIT not in scripts or len(data) != 1:
        return ()
    return root + INIT, data[0]


def calculate(initializer, payload, urls):
    """Parse data, never evaluate JavaScript or borrow Canadian/total costs."""
    if len(initializer) > 12000 or len(payload) > 24000:
        return None
    code = re.sub(r"\s+", "", initializer)
    if (not re.search(r"totalTuition=tuitionFees\[itemtype\]\[item\]\.tuition\*i;", code)
            or "tuitionFees[itemtype][item].courses<=i" not in code
            or not re.search(r"currency:['\"]CAD['\"]", code)):
        return None
    match = re.fullmatch(r"\s*var\s+data\s*=\s*(\{.*\})\s*;?\s*", payload, re.S)
    if not match:
        return None
    encoded = re.sub(r"([,{]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', match[1])
    encoded = re.sub(r",\s*([}\]])", r"\1", encoded)
    try:
        data = json.loads(encoded)
        tiers = data["international"]
        if not isinstance(tiers, list) or not tiers or len(tiers) > 12:
            return None
        previous = 0
        chosen = None
        for tier in tiers:
            count = tier["courses"]
            if type(count) is not int or not previous < count <= 12:
                return None
            previous = count
            value = Decimal(str(tier["tuition"]))
            if isinstance(tier["tuition"], bool) or not value.is_finite() or value <= 0:
                return None
            if count <= 4:
                chosen = value
        if chosen is None:
            return None
        amount = (chosen * 4).quantize(Decimal(".01"))
        if not Decimal("100") <= amount <= Decimal("200000"):
            return None
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return None
    quote = (f"Cost for this program per semester: International. "
             f"Calculated tuition: CAD {chosen} per course × 4 courses = CAD {amount} per semester. "
             "Fees, books and supplies excluded. Fee year not stated in calculator data.")
    return ExtractionResult(
        field_key="international_fee", value=float(amount),
        normalized={"international_fee": float(amount), "currency": "CAD",
                    "fee_term": "Semester", "fee_year": None},
        confidence=1.0, method="fee.tru_international_calculator_four_courses",
        snippet=quote, extras={
            "source_urls": list(urls), "courses_per_semester": 4,
            "international_tuition_per_course": str(chosen),
            "calculated": True, "fee_year_verified": False,
        },
    )


async def read_dependency(target, home):
    """Share a permitted public dependency only within this bounded batch."""
    from app.services.scrape_assistant_tools import public_request
    from app.services.scrape_assistant_tools import official_url
    from app.services.official_source_redirects import read_official_source
    target = official_url(target, home)
    async def download():
        raw, final, _ = await read_official_source(target, home, public_request)
        if final != target:
            raise ValueError("Calculator asset moved; needs verification.")
        return raw.decode("utf-8", errors="strict")
    reads = _ASSET_READS.get()
    if reads is None:
        return await download()
    if target not in reads:
        reads[target] = asyncio.create_task(download())
    return await asyncio.shield(reads[target])


async def extract(html, url):
    selected = assets(html, url)
    if selected is None:
        return False, None
    if not selected:
        return True, None
    try:
        documents = await asyncio.wait_for(
            asyncio.gather(*(read_dependency(u, url) for u in selected), return_exceptions=True), timeout=10)
    except (ValueError, OSError, TimeoutError, UnicodeError):
        return True, None
    if any(isinstance(value, BaseException) for value in documents):
        return True, None
    initializer, payload = documents
    return True, calculate(initializer, payload, selected)
