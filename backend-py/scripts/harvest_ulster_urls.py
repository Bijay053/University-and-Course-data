#!/usr/bin/env python3
"""
Harvest all Ulster University course URLs from Funnelback search via Scrape.do render.

Usage:
  cd backend-py && PYTHONPATH=. python3 scripts/harvest_ulster_urls.py

Run when the Ulster sitemap URL is inaccessible (Scrape.do 502 for sitemap XML)
but Scrape.do render still works for regular Ulster pages. The Funnelback search
result pages embed course URLs inside squiz.cloud redirect hrefs.

Output: scraper_config/unis/ulster_2176_course_urls.txt
        (one URL per line, both 202627 and 202728 included;
         block_url_patterns in ulster_2176.yaml filters 202728 at runtime)
"""
import httpx, os, re, sys, time
from urllib.parse import unquote

TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")
if not TOKEN:
    sys.exit("SCRAPE_DO_TOKEN env var not set")

OUT_FILE = os.path.join(os.path.dirname(__file__), "../scraper_config/unis/ulster_2176_course_urls.txt")
NUM_RANKS = 100


def fetch_page(start_rank: int) -> list[str]:
    url = (
        f"https://www.ulster.ac.uk/courses?query=%21showall"
        f"&num_ranks={NUM_RANKS}&start_rank={start_rank}"
    )
    try:
        r = httpx.get(
            "https://api.scrape.do",
            params={"token": TOKEN, "url": url, "render": "true", "geoCode": "GB", "waitFor": "8000"},
            timeout=100,
        )
    except Exception as e:
        print(f"  start={start_rank}: request error {e}")
        return []
    if r.status_code != 200:
        print(f"  start={start_rank}: HTTP {r.status_code}")
        return []
    html = r.text
    redirect_urls = re.findall(r"squiz\.cloud/s/redirect\?[^\"\'<\s]+", html)
    courses = []
    for ru in redirect_urls:
        ru_unescaped = ru.replace("&amp;", "&")
        m = re.search(r"[?&]url=([^&]+)", ru_unescaped)
        if m:
            decoded = unquote(m.group(1))
            if "/courses/20" in decoded:
                courses.append(decoded)
    yr27 = sum(1 for u in courses if "202627" in u)
    yr28 = sum(1 for u in courses if "202728" in u)
    print(f"  start={start_rank}: {len(courses)} URLs (202627={yr27}, 202728={yr28})")
    return courses


def main() -> None:
    all_courses: set[str] = set()
    for start in range(1, 1500, NUM_RANKS):
        urls = fetch_page(start)
        if not urls:
            print(f"  No results at start={start}, stopping")
            break
        all_courses.update(urls)
        time.sleep(1)

    by_year: dict[str, int] = {}
    for u in all_courses:
        m = re.search(r"/courses/(\d{6})/", u)
        if m:
            by_year[m.group(1)] = by_year.get(m.group(1), 0) + 1

    print(f"\nTotal unique: {len(all_courses)}")
    for y, c in sorted(by_year.items()):
        print(f"  {y}: {c}")

    with open(OUT_FILE, "w") as f:
        f.write("# Harvested from Ulster Funnelback (all modes) via Scrape.do render\n")
        f.write("# Refresh: cd backend-py && python3 scripts/harvest_ulster_urls.py\n")
        f.write("# Both 202627 and 202728 are included; block_url_patterns in\n")
        f.write("# ulster_2176.yaml filters 202728 variants at runtime.\n")
        for url in sorted(all_courses):
            f.write(url + "\n")
    print(f"Written {len(all_courses)} URLs to {OUT_FILE}")


if __name__ == "__main__":
    main()
