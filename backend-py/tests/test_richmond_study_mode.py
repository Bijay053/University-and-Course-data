import asyncio

from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors import study_mode


def _richmond_config():
    config = load_uni_config(
        slug="richmond",
        scrape_url="https://www.richmond.ac.uk",
        university_id=90,
        name="Richmond American University London",
    )
    set_uni_config(config)
    return config


def test_richmond_requires_strong_online_delivery_evidence():
    config = _richmond_config()
    assert (
        config.extraction.study_mode.online_only_requires_strong_evidence
        is True
    )

    html = """
    <html><body>
      <header>Student Support Online Learning Resources</header>
      <main>
        <h1>BA (Hons) International History Degree Programme</h1>
        <a>Apply online today</a>
        <p>Students join field trips and guest lectures.</p>
        <p>Guided learning includes online discussion boards and workshops.</p>
        <strong>12 hours of contact time per week</strong>
      </main>
    </body></html>
    """

    result = asyncio.run(
        study_mode.extract(
            html,
            (
                "https://www.richmond.ac.uk/undergraduate-programmes/"
                "ba-international-history-2025/"
            ),
        )
    )

    assert result == []


def test_richmond_still_rejects_explicit_online_delivery():
    _richmond_config()
    html = """
    <html><body><main>
      <h1>Example Online Programme</h1>
      <p>Delivery mode: Online</p>
    </main></body></html>
    """

    result = asyncio.run(
        study_mode.extract(
            html,
            "https://www.richmond.ac.uk/undergraduate-programmes/example-online/",
        )
    )

    assert len(result) == 1
    assert result[0].value == "Online"
    assert result[0].method == "study_mode:label"