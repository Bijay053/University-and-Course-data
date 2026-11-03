"""Password recovery never discloses reset credentials in HTTP or logs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import auth
from app.services import email

TOKEN = "test-only-reset-credential-not-a-real-token"


@pytest.fixture
def recovery(monkeypatch):
    db = MagicMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    loader = AsyncMock(return_value=SimpleNamespace(
        id=123, email="person@example.test", full_name="Test", is_active=True,
    ))
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "_load_user_by_email", loader)
    monkeypatch.setattr(auth, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(auth, "send_email", sender)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _: TOKEN)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[auth.get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db, loader, sender


def test_missing_sender_fails_before_account_lookup_or_token_creation(recovery, monkeypatch, caplog):
    client, db, loader, sender = recovery
    monkeypatch.setattr(auth, "smtp_is_configured", lambda: False)
    responses = [
        client.post("/api/auth/forgot-password", json={"email": address})
        for address in ["person@example.test", "unknown@example.test"]
    ]
    assert responses[0].status_code == responses[1].status_code == 503
    assert responses[0].json() == responses[1].json()
    assert "unavailable" in responses[0].json()["detail"]
    assert TOKEN not in responses[0].text + caplog.text
    loader.assert_not_awaited()
    sender.assert_not_awaited()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


def test_sent_reset_link_is_only_in_email_and_never_public_response(recovery, caplog):
    client, db, _, sender = recovery
    result = client.post("/api/auth/forgot-password", json={"email": "person@example.test"})
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert "debug_reset_url" not in result.json()
    assert TOKEN not in result.text + caplog.text
    assert TOKEN in sender.call_args.args[2]
    assert TOKEN in sender.call_args.kwargs["html_body"]
    assert "Reset password" in sender.call_args.kwargs["html_body"]
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("account_state", ["unknown", "inactive", "active"])
def test_account_states_and_delivery_failure_share_acknowledgement(recovery, account_state, caplog):
    client, db, loader, sender = recovery
    if account_state == "unknown":
        loader.return_value = None
    elif account_state == "inactive":
        loader.return_value.is_active = False
    sender.return_value = False
    result = client.post("/api/auth/forgot-password", json={"email": "person@example.test"})
    assert result.status_code == 200
    assert result.json() == {
        "ok": True,
        "message": "If an active account uses this address, check its inbox for reset instructions. "
                   "If no email arrives, contact the portal administrator.",
    }
    assert TOKEN not in result.text + caplog.text
    if account_state == "active":
        db.delete.assert_awaited_once_with(db.add.call_args.args[0])
        assert db.commit.await_count == 2
    else:
        sender.assert_not_awaited()
        db.add.assert_not_called()


@pytest.mark.asyncio
async def test_email_fallback_never_logs_message_body(monkeypatch, caplog):
    monkeypatch.setattr(email, "_is_configured", lambda: False)
    assert await email.send_email("person@example.test", "Reset", TOKEN) is False
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_transport_error_cannot_echo_credentials_into_logs(monkeypatch, caplog):
    monkeypatch.setattr(email, "_is_configured", lambda: True)

    def fail(*args):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(email, "_send_sync", fail)
    assert await email.send_email("person@example.test", "Reset", TOKEN) is False
    assert TOKEN not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("html_body", [None, "<html><body>Test HTML email</body></html>"])
def test_ses_uses_dedicated_identity_and_verified_sender(monkeypatch, html_body):
    import boto3

    for key, value in {
        "EMAIL_TRANSPORT": "ses",
        "SES_ACCESS_ID": "test-ses-identity", "SES_SECRET_KEY": "test-ses-secret",
        "SES_REGION": "ap-southeast-2", "DEFAULT_FROM_EMAIL": "sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    client = MagicMock()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(boto3, "client", factory)
    assert email._is_configured()
    email._send_sync("person@example.test", "Reset", TOKEN, html_body)
    assert factory.call_args.kwargs["aws_access_key_id"] == "test-ses-identity"
    assert factory.call_args.kwargs["aws_secret_access_key"] == "test-ses-secret"
    assert factory.call_args.kwargs["region_name"] == "ap-southeast-2"
    request = client.send_email.call_args.kwargs
    assert request["Source"] == "sender@example.test"
    assert request["Destination"] == {"ToAddresses": ["person@example.test"]}
    assert request["Message"]["Body"]["Text"]["Data"] == TOKEN
    if html_body:
        assert request["Message"]["Body"]["Html"] == {"Data": html_body, "Charset": "UTF-8"}
    else:
        assert "Html" not in request["Message"]["Body"]
    client.close.assert_called_once()


@pytest.mark.parametrize("html_body", [None, "<html><body>Test HTML email</body></html>"])
def test_smtp_supports_supplied_credential_aliases(monkeypatch, html_body):
    monkeypatch.setenv("EMAIL_TRANSPORT", "smtp")
    monkeypatch.setattr(email, "_ses_is_configured", lambda: False)
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "EMAIL_HOST": "smtp.example.test", "EMAIL_PORT": "587",
        "EMAIL_HOST_USER": "test-smtp-identity", "EMAIL_HOST_PASSWORD": "test-smtp-secret",
        "DEFAULT_FROM_EMAIL": "sender@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    connection = MagicMock()
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = connection
    monkeypatch.setattr(email.smtplib, "SMTP", smtp)
    assert email._is_configured()
    email._send_sync("person@example.test", "Reset", TOKEN, html_body)
    connection.starttls.assert_called_once()
    connection.login.assert_called_once_with("test-smtp-identity", "test-smtp-secret")
    message = connection.send_message.call_args.args[0]
    assert message["From"] == "sender@example.test"
    assert TOKEN in message.get_body(preferencelist=("plain",)).get_content()
    if html_body:
        assert message.get_content_type() == "multipart/alternative"
        assert html_body in message.get_body(preferencelist=("html",)).get_content()
    else:
        assert not message.is_multipart()
