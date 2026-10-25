"""Logging for every step: stderr (Airflow shows it in the task log) + logs/pipeline.log."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_configured = False


def setup_logging(log_dir: Path | None = None, level: str | None = None) -> None:
    """Configure the root logger once. Level comes from PANTRYPATH_LOG_LEVEL (default INFO)."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level or os.environ.get("PANTRYPATH_LOG_LEVEL", "INFO"))
    formatter = logging.Formatter(FORMAT)

    stream = logging.StreamHandler()  # stderr, so stdout stays free for the step's JSON summary
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "pipeline.log", maxBytes=5_000_000, backupCount=3
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for noisy in ("urllib3", "httpx", "filelock", "great_expectations"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
