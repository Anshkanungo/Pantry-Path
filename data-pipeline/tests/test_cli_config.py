"""The step runner, config loading and alerts."""

import json
import logging

import pytest

from pantrypath_pipeline import alerts, cli
from pantrypath_pipeline.config import load_config, secret
from pantrypath_pipeline.errors import DataUnavailableError


def test_config_resolves_paths_against_home(tmp_path):
    cfg = load_config(home=tmp_path.parent, path=cli.__file__.replace(
        "src/pantrypath_pipeline/cli.py", "config/pipeline.yaml"))
    assert cfg.path("data/raw") == tmp_path.parent / "data/raw"
    assert cfg.path("/abs/file") .as_posix() == "/abs/file"
    assert sum(cfg["catalog"]["quotas"].values()) == 100


def test_secret_treats_blank_as_missing(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "  ")
    assert secret("SOME_KEY") is None


def test_every_step_points_at_a_module_function():
    for step, target in cli.STEPS.items():
        module, func = target.split(":")
        assert module.startswith("pantrypath_pipeline.") and func, step


def test_step_prints_json_summary(monkeypatch, capsys, cfg):
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "resolve", lambda step: lambda c, **kw: {"rows": 3, **kw})
    assert cli.main(["refine-dish", "--dish-id", "lasagna"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "rows": 3, "dish_id": "lasagna"}


def test_step_failure_returns_1_and_alerts(monkeypatch, cfg):
    sent = []

    def boom(c, **kw):
        raise DataUnavailableError("Kaggle unreachable")

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "resolve", lambda step: boom)
    monkeypatch.setattr(cli, "send_alert", lambda *a, **kw: sent.append(a))
    assert cli.main(["download-foodcom"]) == 1
    assert sent and "download-foodcom" in sent[0][0]


def test_step_that_already_alerted_is_not_alerted_twice(monkeypatch, cfg):
    sent = []

    def boom(c, **kw):
        exc = DataUnavailableError("validation failed; details already emailed")
        exc.alerted = True
        raise exc

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "resolve", lambda step: boom)
    monkeypatch.setattr(cli, "send_alert", lambda *a, **kw: sent.append(a))
    assert cli.main(["validate-off"]) == 1
    assert sent == []


def test_alert_without_email_is_logged_only(cfg, caplog):
    with caplog.at_level(logging.INFO):
        assert alerts.send_alert("row count dropped", "off: -35%", cfg=cfg) is False
    assert "row count dropped" in caplog.text
    assert "ALERT_SMTP_PASSWORD" in caplog.text  # says what's missing


def test_alert_recipient_comes_from_config_and_env_overrides_it(cfg, monkeypatch):
    assert alerts.email_settings(cfg)[0] == cfg["alerts"]["email_to"]
    assert alerts.email_configured(cfg) is False  # no password in tests
    monkeypatch.setenv("ALERT_EMAIL_TO", "team@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "app-password")
    assert alerts.email_settings(cfg)[0] == "team@example.com"
    assert alerts.email_configured(cfg) is True


def test_alert_is_emailed_when_configured(cfg, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    assert alerts.send_alert("drift", "rows -35%", level="error", cfg=cfg) is True
    to = cfg["alerts"]["email_to"]
    assert sent == [("connect", "smtp.gmail.com", 587), ("login", cfg["alerts"]["smtp_user"]),
                    ("send", to, "[Pantry Path pipeline] ERROR: drift")]


def test_unknown_step_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["no-such-step"])
