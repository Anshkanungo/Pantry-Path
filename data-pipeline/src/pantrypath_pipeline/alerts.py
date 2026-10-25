"""Alerts: always logged; also emailed (Gmail SMTP) when a password is available.

Recipient and sender come from config/pipeline.yaml (alerts.email_to, alerts.smtp_user);
ALERT_EMAIL_TO (comma-separated) and ALERT_SMTP_USER in .env override them, and
ALERT_EMAIL_FROM sets a different From address. The one secret, ALERT_SMTP_PASSWORD
(a Gmail app password), lives only in .env. Sending an alert never raises: a broken
mail setup must not crash the pipeline.
"""

import logging
import smtplib
from email.message import EmailMessage

from pantrypath_pipeline.config import Config, load_config, secret

log = logging.getLogger(__name__)
LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def email_settings(cfg: Config) -> tuple[str | None, str | None, str | None]:
    """(recipient, smtp user, smtp password): .env first, then the config file."""
    settings = cfg.get("alerts", {})
    to = secret("ALERT_EMAIL_TO") or settings.get("email_to")
    user = secret("ALERT_SMTP_USER") or settings.get("smtp_user")
    return to, user, secret("ALERT_SMTP_PASSWORD")


def email_configured(cfg: Config) -> bool:
    return cfg.get("alerts", {}).get("email_enabled", True) and all(email_settings(cfg))


def send_alert(
    subject: str, body: str, *, level: str = "warning", cfg: Config | None = None
) -> bool:
    """Log the alert, then email it if configured. Returns True when an email was sent."""
    log.log(LEVELS.get(level, logging.WARNING), "ALERT %s: %s | %s", level.upper(), subject, body)
    try:
        cfg = cfg or load_config()
        to, user, password = email_settings(cfg)
        if not email_configured(cfg):
            log.info("Alert not emailed%s: set ALERT_SMTP_PASSWORD (a Gmail app password) in "
                     ".env to send alerts by email", f" to {to}" if to else "")
            return False
        settings = cfg.get("alerts", {})
        msg = EmailMessage()
        prefix = settings.get("subject_prefix", "[Pantry Path pipeline]")
        msg["Subject"] = f"{prefix} {level.upper()}: {subject}"
        msg["From"] = secret("ALERT_EMAIL_FROM") or user
        msg["To"] = to
        msg.set_content(body)
        host = settings.get("smtp_host", "smtp.gmail.com")
        port = int(settings.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        log.info("Alert emailed to %s: %s", to, subject)
        return True
    except Exception:  # noqa: BLE001 - alerting must never take the pipeline down
        log.exception("Could not email alert %r; it is in the log above", subject)
        return False
