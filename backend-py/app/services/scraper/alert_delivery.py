"""Priority 5 — Component 4: alert delivery.

Persists alerts are already in the DB (done by evaluate_run_alerts).
This module handles out-of-band delivery for critical severity only:
  - Slack webhook (SLACK_WEBHOOK_URL env var)
  - Email via SMTP (ALERT_EMAIL_TO + SMTP_* env vars)

Warnings stay on the dashboard only; this is intentional — only critical
issues that require immediate human action get pushed out.

If neither SLACK_WEBHOOK_URL nor ALERT_EMAIL_TO is set this module is a
no-op, which is fine for environments that use the dashboard exclusively.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
import urllib.error
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.scrape_run_alert import ScrapeRunAlert

log = logging.getLogger(__name__)

# Week 2 P4: explicit master switch so notifications can be muted during
# development / shadow runs without removing the env vars themselves.
# Set ``ALERTS_NOTIFICATION_ENABLED=false`` to suppress Slack + email
# delivery while still persisting alerts to the DB.  Default is true so
# production keeps the existing behaviour.
def _notifications_enabled() -> bool:
    val = os.environ.get("ALERTS_NOTIFICATION_ENABLED", "true").strip().lower()
    return val not in {"false", "0", "no", "off"}


SLACK_WEBHOOK_URL: str | None = os.environ.get("SLACK_WEBHOOK_URL")
ALERT_EMAIL_TO: str | None = os.environ.get("ALERT_EMAIL_TO")
SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER: str | None = os.environ.get("SMTP_USER")
SMTP_PASSWORD: str | None = os.environ.get("SMTP_PASSWORD")
SMTP_FROM: str = os.environ.get("SMTP_FROM", "scraper-alerts@noreply.local")


def format_alert_digest(scrape_run_id: str, alerts: list[ScrapeRunAlert]) -> str:
    lines = [f"Scrape run {scrape_run_id} — {len(alerts)} critical alert(s):\n"]
    for a in alerts:
        lines.append(f"  • [{a.rule_id}] {a.message}")
    lines.append(f"\nReview: check v_active_alerts / v_university_health, or scrape_run_alerts where scrape_run_id='{scrape_run_id}'")
    return "\n".join(lines)


async def deliver_alerts(alerts: list[ScrapeRunAlert]) -> None:
    """Send critical alerts to Slack / email.  Warnings stay on dashboard only."""
    critical = [a for a in alerts if a.severity == "critical"]
    if not critical:
        return
    if not _notifications_enabled():
        log.info(
            "[ALERT DELIVERY] ALERTS_NOTIFICATION_ENABLED=false — "
            "muting %d critical alert(s) (still persisted to DB)",
            len(critical),
        )
        return

    # Group by run ID (normally all from one run, but handle multiple)
    by_run: dict[str, list[ScrapeRunAlert]] = {}
    for a in critical:
        by_run.setdefault(a.scrape_run_id, []).append(a)

    for run_id, run_alerts in by_run.items():
        message = format_alert_digest(run_id, run_alerts)

        if SLACK_WEBHOOK_URL:
            _send_slack(SLACK_WEBHOOK_URL, run_id, message)

        if ALERT_EMAIL_TO and SMTP_HOST:
            _send_email(
                to=ALERT_EMAIL_TO,
                subject=f"[Scraper CRITICAL] {len(run_alerts)} alert(s) in run {run_id}",
                body=message,
            )


# ---------------------------------------------------------------------------
# Transport helpers (sync — acceptable here; delivery is fire-and-forget)
# ---------------------------------------------------------------------------

def _send_slack(webhook_url: str, run_id: str, message: str) -> None:
    payload = json.dumps({"text": message}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        log.info("[ALERT DELIVERY] Slack OK for run %s (HTTP %s)", run_id, status)
    except urllib.error.URLError as exc:
        log.warning("[ALERT DELIVERY] Slack failed for run %s: %s", run_id, exc)


def deliver_discovery_failure_alert(
    uni_name: str,
    uni_id: int,
    scrape_url: str,
    candidates_found: int,
    diagnostic: dict,
) -> None:
    """Fire-and-forget Slack/email when discovery yields < 3 candidates.

    Wraps the raw transport helpers so the orchestrator can call this with
    a single line.  No-ops gracefully when neither transport is configured.
    """
    subject = f"[Tier-7] Discovery failure — {uni_name} ({candidates_found} candidate(s))"
    lines = [
        f"University:  {uni_name}  (id={uni_id})",
        f"Scrape URL:  {scrape_url}",
        f"Candidates:  {candidates_found}  (threshold < 3)",
        "",
        "All discovery tiers (BFS, sitemap, alt-paths, subdomain probes,",
        "browser fallback, Wayback Machine) returned fewer than 3 course links.",
        "This is almost always caused by:",
        "  • The site blocking the crawler (403 / Cloudflare / geo-block)",
        "  • scrape_url changed or is no longer valid",
        "  • A Playwright / network outage on the scraper host",
        "",
        "Diagnostic snapshot:",
    ]
    for key, val in diagnostic.items():
        lines.append(f"  {key}: {val}")
    lines.append("")
    lines.append(
        "Action: check the scrape job log in the admin UI, then resolve "
        "the discovery_failure_alerts row once the root cause is fixed."
    )
    body = "\n".join(lines)

    if not _notifications_enabled():
        log.info("[ALERT DELIVERY] ALERTS_NOTIFICATION_ENABLED=false — muting discovery-failure alert")
        return

    if SLACK_WEBHOOK_URL:
        _send_slack_raw(SLACK_WEBHOOK_URL, subject, body)

    if ALERT_EMAIL_TO and SMTP_HOST:
        _send_email(to=ALERT_EMAIL_TO, subject=subject, body=body)


def deliver_drift_alert(
    *,
    before_date: str,
    after_date: str,
    diffs: list[dict],
    warnings: list[dict],
    summary: str,
) -> None:
    """Send the nightly regression-sweep drift report via Slack/email.

    Called by the ``scrape.nightly_sweep`` Celery beat task when the
    before/after baseline comparison finds unexpected changes.

    ``diffs`` and ``warnings`` are lists of dict produced by the sweep
    (each has ``slug``, ``field``, ``before``, ``after`` keys).
    """
    if not diffs and not warnings:
        log.info("[DRIFT ALERT] no diffs — skipping delivery")
        return

    subject = (
        f"[Nightly Drift] {len(diffs)} error(s), {len(warnings)} warning(s) "
        f"— {before_date} → {after_date}"
    )
    lines = [
        f"Nightly regression sweep:  {before_date} → {after_date}",
        summary,
        "",
    ]
    if diffs:
        lines.append(f"ERRORS ({len(diffs)}):")
        for d in diffs[:30]:  # cap at 30 to keep message readable
            lines.append(
                f"  [{d.get('slug','')}] {d.get('field','')}: "
                f"{d.get('before','')} → {d.get('after','')}"
            )
        if len(diffs) > 30:
            lines.append(f"  … and {len(diffs) - 30} more")
        lines.append("")
    if warnings:
        lines.append(f"WARNINGS ({len(warnings)}):")
        for w in warnings[:20]:
            lines.append(
                f"  [{w.get('slug','')}] {w.get('field','')}: "
                f"{w.get('before','')} → {w.get('after','')}"
            )
        if len(warnings) > 20:
            lines.append(f"  … and {len(warnings) - 20} more")
        lines.append("")
    lines.append(
        "Review: compare baselines/nightly/<before_date>/ vs baselines/nightly/<after_date>/"
    )
    body = "\n".join(lines)

    if not _notifications_enabled():
        log.info("[ALERT DELIVERY] ALERTS_NOTIFICATION_ENABLED=false — muting drift alert")
        return

    if SLACK_WEBHOOK_URL:
        _send_slack_raw(SLACK_WEBHOOK_URL, subject, body)

    if ALERT_EMAIL_TO and SMTP_HOST:
        _send_email(to=ALERT_EMAIL_TO, subject=subject, body=body)


def _send_slack_raw(webhook_url: str, subject: str, body: str) -> None:
    """Post an arbitrary text message to Slack."""
    payload = json.dumps({"text": f"*{subject}*\n```{body}```"}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        log.info("[ALERT DELIVERY] Slack OK (HTTP %s)", status)
    except urllib.error.URLError as exc:
        log.warning("[ALERT DELIVERY] Slack failed: %s", exc)


def _send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST:
        log.debug("[ALERT DELIVERY] SMTP_HOST not set — skipping email")
        return
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            if SMTP_USER and SMTP_PASSWORD:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        log.info("[ALERT DELIVERY] email sent to %s", to)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ALERT DELIVERY] email failed: %s", exc)
