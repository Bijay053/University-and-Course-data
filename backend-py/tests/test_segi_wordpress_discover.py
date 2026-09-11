from __future__ import annotations

import html
import json

import pytest

from app.services.scraper import segi_wordpress_discover as segi_wp


@pytest.mark.asyncio
async def test_wordpress_supplement_uses_one_rendered_attempt_and_strict_urls(
    monkeypatch,
) -> None:
    calls = []
    items = [
        {
            "id": 1,
            "subtype": "page",
            "title": "Diploma in Occupational Safety &amp; Health",
            "url": (
                "https://www.segi.edu.my/"
                "diploma-in-occupational-safety-and-health-kl/"
            ),
        },
        {
            "id": 2,
            "subtype": "post",
            "title": "Bachelor campus news",
            "url": "https://www.segi.edu.my/campus-news/",
        },
        {
            "id": 3,
            "subtype": "page",
            "title": "Nested page",
            "url": "https://www.segi.edu.my/study/course/",
        },
        {
            "id": 4,
            "subtype": "page",
            "title": "Foreign result",
            "url": "https://attacker.example/course/",
        },
        {
            "id": 5,
            "subtype": "page",
            "title": "SEGi Scholarships",
            "url": "https://www.segi.edu.my/segi-scholarships/",
        },
    ]

    async def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return (
            "<html><body><pre>"
            + html.escape(json.dumps(items))
            + "</pre></body></html>"
        )

    monkeypatch.setattr(segi_wp, "fetch_html_scrape_do", fake_fetch)

    links = await segi_wp.discover_segi_wordpress_courses()

    assert links == [{
        "name": "Diploma in Occupational Safety & Health",
        "url": (
            "https://www.segi.edu.my/"
            "diploma-in-occupational-safety-and-health-kl/"
        ),
    }]
    assert len(calls) == 1
    assert calls[0][1] == {"render": True, "max_retries": 0}
    assert "search=Programme+ID" in calls[0][0]