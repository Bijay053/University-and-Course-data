"""Email delivery for account recovery.

Supports dedicated Amazon SES credentials or SMTP. Missing settings or failures return
False. Message bodies are never logged: they can contain reset credentials.

Amazon SES:
    SES_ACCESS_ID, SES_SECRET_KEY, SES_REGION, DEFAULT_FROM_EMAIL
    The sender must be verified in this SES region.

SMTP (EMAIL_HOST/EMAIL_PORT/EMAIL_HOST_USER/EMAIL_HOST_PASSWORD aliases supported):
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USER       login user
    SMTP_PASSWORD   login password / app password
    SMTP_FROM       From: address (defaults to SMTP_USER if missing)

Optional:
    EMAIL_TRANSPORT "ses" or "smtp"; otherwise prefer configured SES
    SMTP_USE_TLS    "true"/"false" -- defaults to true on port != 465
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("uniportal.email")


def _ses_is_configured() -> bool:
    if os.environ.get("EMAIL_TRANSPORT", "").lower() == "smtp":
        return False
    return all(os.environ.get(k) for k in (
        "SES_ACCESS_ID", "SES_SECRET_KEY", "SES_REGION", "DEFAULT_FROM_EMAIL",
    ))


def _is_configured() -> bool:
    if _ses_is_configured():
        return True
    if os.environ.get("EMAIL_TRANSPORT", "").lower() == "ses":
        return False
    return all((
        os.environ.get("SMTP_HOST") or os.environ.get("EMAIL_HOST"),
        os.environ.get("SMTP_PORT") or os.environ.get("EMAIL_PORT"),
        os.environ.get("SMTP_USER") or os.environ.get("EMAIL_HOST_USER"),
        os.environ.get("SMTP_PASSWORD") or os.environ.get("EMAIL_HOST_PASSWORD"),
    ))


def _send_sync(to: str, subject: str, body: str, html_body: str | None = None) -> None:
    if _ses_is_configured():
        import boto3
        from botocore.config import Config

        # Dedicated SES identity only; never borrow the snapshot/S3 identity.
        client = boto3.client(
            "ses", region_name=os.environ["SES_REGION"],
            aws_access_key_id=os.environ["SES_ACCESS_ID"],
            aws_secret_access_key=os.environ["SES_SECRET_KEY"],
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
        )
        message_body = {"Text": {"Data": body, "Charset": "UTF-8"}}
        if html_body:
            message_body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
        try:
            client.send_email(
                Source=os.environ["DEFAULT_FROM_EMAIL"],
                Destination={"ToAddresses": [to]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": message_body,
                },
            )
        finally:
            client.close()
        return

    host = os.environ.get("SMTP_HOST") or os.environ["EMAIL_HOST"]
    port = int(os.environ.get("SMTP_PORT") or os.environ["EMAIL_PORT"])
    user = os.environ.get("SMTP_USER") or os.environ["EMAIL_HOST_USER"]
    password = os.environ.get("SMTP_PASSWORD") or os.environ["EMAIL_HOST_PASSWORD"]
    sender = os.environ.get("SMTP_FROM") or os.environ.get("DEFAULT_FROM_EMAIL") or user
    use_tls = os.environ.get("SMTP_USE_TLS", os.environ.get("EMAIL_USE_TLS", "true")).lower() != "false"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if use_tls:
                s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


async def send_email(to: str, subject: str, body: str, *, html_body: str | None = None) -> bool:
    """Send an email asynchronously. Returns True on success.

    Never expose the message body or transport exception text in logs.
    """
    if not _is_configured():
        log.warning("Email delivery unavailable: sender settings are incomplete")
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body, html_body)
        log.info("Sent email to %s (subject=%r)", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001 -- we intentionally swallow + log
        log.error("Email delivery failed (%s)", type(exc).__name__)
        return False
