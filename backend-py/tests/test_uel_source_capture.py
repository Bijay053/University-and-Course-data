import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/uel_source_capture.py"
spec = importlib.util.spec_from_file_location("uel_source_capture_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SourceCapture = module.SourceCapture
is_uel_detail_source = module.is_uel_detail_source


@pytest.mark.asyncio
async def test_records_html_without_changing_transport_arguments_or_result(tmp_path):
    calls = []
    expected = "<html><body>browser fallback</body></html>"

    async def real_fetch(url, *, wait_until, timeout):
        calls.append((url, wait_until, timeout))
        return expected

    capture = SourceCapture(tmp_path)
    result = await capture.wrap("browser", real_fetch)(
        "https://www.uel.ac.uk/postgraduate/courses/"
        "pgcert-autism-spectrum-conditions-learning",
        wait_until="domcontentloaded",
        timeout=30_000,
    )

    assert result is expected
    assert calls == [
        (
            "https://www.uel.ac.uk/postgraduate/courses/"
            "pgcert-autism-spectrum-conditions-learning",
            "domcontentloaded",
            30_000,
        )
    ]
    assert capture.manifest[0]["transport"] == "browser"
    assert capture.manifest[0]["source_kind"] == "course_detail"
    assert (tmp_path / capture.manifest[0]["file"]).read_text() == expected
    assert capture.attempts[0]["status"] == "html"


@pytest.mark.asyncio
async def test_preserves_non_html_sentinel_identity_and_records_it(tmp_path):
    sentinel = object()

    async def real_fetch(url):
        return sentinel

    capture = SourceCapture(tmp_path)
    result = await capture.wrap("browser", real_fetch)("https://example.invalid")

    assert result is sentinel
    assert capture.manifest == []
    assert capture.attempts[0]["status"] == "no_html"
    assert capture.attempts[0]["result_type"] == "object"


@pytest.mark.asyncio
async def test_records_and_reraises_same_transport_exception(tmp_path):
    failure = RuntimeError("real transport failed")

    async def real_fetch(url):
        raise failure

    capture = SourceCapture(tmp_path)
    with pytest.raises(RuntimeError) as raised:
        await capture.wrap("general_http", real_fetch)("https://example.invalid")

    assert raised.value is failure
    assert capture.errors == [
        {
            "url": "https://example.invalid",
            "transport": "general_http",
            "status": "error",
            "error_type": "RuntimeError",
            "elapsed_seconds": capture.errors[0]["elapsed_seconds"],
        }
    ]
    assert (tmp_path / "html-fetch-attempts.json").exists()


@pytest.mark.asyncio
async def test_supporting_html_is_separate_from_reporter_detail_manifest(tmp_path):
    async def real_fetch(url):
        return "<html>listing</html>"

    capture = SourceCapture(
        tmp_path,
        is_detail_source=lambda url: "/courses/" in url,
    )
    await capture.wrap("browser", real_fetch)(
        "https://www.uel.ac.uk/study/postgraduate/courses"
    )

    assert capture.manifest == []
    assert capture.source_manifest[0]["source_kind"] == "supporting"
    assert (tmp_path / "source-html-manifest.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.uel.ac.uk/postgraduate/courses/"
        "pgcert-autism-spectrum-conditions-learning",
        "https://www.uel.ac.uk/undergraduate/courses/computer-science-bsc-hons",
        "https://www.uel.ac.uk/undergraduate/courses/computer-science-bsc-hons/",
    ],
)
def test_real_uel_course_detail_urls_are_classified(url):
    assert is_uel_detail_source(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.uel.ac.uk/study/undergraduate/courses",
        "https://www.uel.ac.uk/study/postgraduate/courses",
        "https://www.uel.ac.uk/postgraduate/courses",
        "https://uel.ac.uk/postgraduate/courses/example",
        "https://www.uel.ac.uk.evil.invalid/postgraduate/courses/example",
        "https://example.invalid/postgraduate/courses/example",
    ],
)
def test_listings_and_non_exact_uel_hosts_are_supporting(url):
    assert not is_uel_detail_source(url)