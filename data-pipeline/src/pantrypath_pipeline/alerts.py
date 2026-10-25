"""Alerts: always logged; also emailed (Gmail SMTP) when configured in .env.

Variables: ALERT_EMAIL_TO (comma-separated), ALERT_SMTP_USER, ALERT_SMTP_PASSWORD
(a Gmail app password), optional ALERT_EMAIL_FROM (defaults to the SMTP user).
Sending an alert never raises: a broken mail setup must not crash the pipeline.
"""

import logging
import smtplib
from email.message import EmailMessage

from pantrypath_pipeline.config import Config, load_config, secret

log = logging.getLogger(__name__)
LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def email_configured() -> bool:
    return all(secret(n) for n in ("ALERT_EMAIL_TO", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD"))


def send_alert(
    subject: str, body: str, *, level: str = "warning", cfg: Config | None = None
) -> bool:
    """Log the alert, then email it if configured. Returns True when an email was sent."""
    log.log(LEVELS.get(level, logging.WARNING), "ALERT %s: %s | %s", level.upper(), subject, body)
    try:
        cfg = cfg or load_config()
        settings = cfg.get("alerts", {})
        if not settings.get("email_enabled", True) or not email_configured():
            log.info("Email alerts not configured; alert logged only")
            return False
        msg = EmailMessage()
        prefix = settings.get("subject_prefix", "[Pantry Path pipeline]")
        msg["Subject"] = f"{prefix} {level.upper()}: {subject}"
        msg["From"] = secret("ALERT_EMAIL_FROM") or secret("ALERT_SMTP_USER")
        msg["To"] = secret("ALERT_EMAIL_TO")
        msg.set_content(body)
        host = settings.get("smtp_host", "smtp.gmail.com")
        port = int(settings.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(secret("ALERT_SMTP_USER"), secret("ALERT_SMTP_PASSWORD"))
            smtp.send_message(msg)
        log.info("Alert emailed: %s", subject)
        return True
    except Exception:  # noqa: BLE001 - alerting must never take the pipeline down
        log.exception("Could not email alert %r; it is in the log above", subject)
        return False
