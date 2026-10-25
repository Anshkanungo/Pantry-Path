"""Shared test fixtures. Tests never call paid or live APIs and never send email."""

import shutil
from pathlib import Path

import pytest
import yaml

from pantrypath_pipeline.config import Config

PIPELINE_HOME = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch):
    """Hide real keys so no test can make paid LLM calls or send alert emails."""
    for name in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "ALERT_EMAIL_TO",
                 "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "PANTRYPATH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PANTRYPATH_ENV_FILE", "/nonexistent/.env")


@pytest.fixture
def cfg(tmp_path) -> Config:
    """The real pipeline.yaml, with every relative path resolved inside a temp folder."""
    data = yaml.safe_load((PIPELINE_HOME / "config" / "pipeline.yaml").read_text())
    for sub in ("data/raw", "data/interim", "data/processed", "reports/ge", "logs", "results"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    if (PIPELINE_HOME / "prompts").is_dir():
        shutil.copytree(PIPELINE_HOME / "prompts", tmp_path / "prompts")
    return Config(data, home=tmp_path)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
