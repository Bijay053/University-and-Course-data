from unittest.mock import MagicMock

from bs4 import BeautifulSoup
import pytest

from app.services import email
from app.services.email_templates import login_code_email, password_reset_email


def test_login_template_has_clear_copyable_code_and_matching_expiry():
    message = login_code_email("123456", "Test User", expires_minutes=10)
    soup = BeautifulSoup(message.html, "html.parser")
    assert soup.find("h1").get_text() == "Verify your sign-in"
    assert "123456" in soup.get_text()
    assert "123456" in message.text
    assert "10 minutes" in soup.get_text() and "10 minutes" in message.text
    assert "123456" not in soup.find("div", attrs={"aria-hidden": "true"}).get_text()
    assert "One-time use" in soup.get_text()
    assert "Never share" in soup.get_text()
    assert not soup.find(["img", "script", "iframe"])
    assert not soup.find("link")
    assert "max-width:560px" in message.html


def test_reset_template_has_button_fallback_link_and_matching_expiry():
    url = "https://portal.example.test/reset-password?token=fake-test-token&source=email"
    message = password_reset_email(url, "Test User", expires_hours=2)
    soup = BeautifulSoup(message.html, "html.parser")
    links = soup.find_all("a")
    assert links[0].get_text() == "Reset password"
    assert links[0]["href"] == links[1]["href"] == url
    assert links[1].get_text() == url
    assert url in message.text
    assert "2 hours" in soup.get_text() and "2 hours" in message.text
    assert "Email verification is still required" in message.text
    assert not soup.find(["img", "script", "iframe"])


@pytest.mark.parametrize("kind", ["otp", "reset"])
def test_html_escapes_account_names(kind):
    name = '<img src=x onerror="alert(1)"> & Test'
    message = login_code_email("123456", name) if kind == "otp" else password_reset_email(
        "https://portal.example.test/reset-password?token=fake", name,
    )
    assert "&lt;img" in message.html and "&amp; Test" in message.html
    assert not BeautifulSoup(message.html, "html.parser").find("img")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "/reset-password", "https://user:password@example.test/reset"])
def test_reset_template_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        password_reset_email(url)


@pytest.mark.parametrize("code", ["12345", "1234567", "<html>", "123 45"])
def test_login_template_rejects_invalid_codes(code):
    with pytest.raises(ValueError):
        login_code_email(code)


@pytest.mark.asyncio
async def test_async_delivery_preserves_html_without_logging_credentials(monkeypatch, caplog):
    sender = MagicMock()
    monkeypatch.setattr(email, "_is_configured", lambda: True)
    monkeypatch.setattr(email, "_send_sync", sender)
    assert await email.send_email("person@example.test", "Test email", "fake-sensitive-body",
                                  html_body="<p>fake-sensitive-html</p>")
    sender.assert_called_once_with("person@example.test", "Test email",
                                   "fake-sensitive-body", "<p>fake-sensitive-html</p>")
    assert "fake-sensitive-body" not in caplog.text
    assert "fake-sensitive-html" not in caplog.text
