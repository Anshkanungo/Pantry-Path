"""Download the Open Food Facts product dump (Parquet, ~8 GB, refreshed daily) from Hugging Face.

hf_hub_download resumes an interrupted download and skips a file that is already up
to date, so re-running this step is cheap. The file is then checked (size, readable
Parquet footer, the columns the filter needs); a corrupt file is deleted and
downloaded once more.

off.source = "dvc" skips the dump and pulls the already-filtered snapshot (~90 MB)
from the DVC remote instead: the quick path for graders and teammates.
"""

import logging
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
from huggingface_hub.errors import (
    RemoteEntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
    XetDownloadError,
)
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError

log = logging.getLogger(__name__)

# Columns preprocess/off_filter.py reads; a dump without them can't be used
REQUIRED_COLUMNS = (
    "code", "lang", "product_name", "brands", "categories_tags", "countries_tags",
    "allergens_tags", "traces_tags", "ingredients_text", "labels_tags",
    "unique_scans_n", "completeness", "last_modified_t",
)
# Wrong repo/file names won't fix themselves, so they are not retried
NOT_FOUND = (RepositoryNotFoundError, RemoteEntryNotFoundError, RevisionNotFoundError)


def _retryable(exc: BaseException) -> bool:
    """Network trouble, HTTP 429/5xx and broken transfers are worth another try."""
    return isinstance(exc, (OSError, XetDownloadError)) and not isinstance(exc, NOT_FOUND)


def raw_file(cfg: Config) -> Path:
    off = cfg["off"]
    return cfg.path(off["raw_dir"]) / off["hf_file"]


def run(cfg: Config, **_: object) -> dict:
    off = cfg["off"]
    if off.get("source", "download") == "dvc":
        return pull_snapshot_from_dvc(cfg)
    try:
        path = download(cfg)
        try:
            rows = check_raw_file(path, int(off["min_raw_bytes"]))
        except DataCorruptError as exc:
            log.warning("%s; deleting it and downloading once more", exc)
            path.unlink(missing_ok=True)
            path = download(cfg, force=True)
            try:
                rows = check_raw_file(path, int(off["min_raw_bytes"]))
            except DataCorruptError:
                path.unlink(missing_ok=True)
                raise
    except DataUnavailableError as exc:
        previous = cfg.path(off["output"])
        if not previous.exists():
            raise
        send_alert(
            "Open Food Facts download failed; keeping the previous snapshot",
            f"{exc}\nSnapshot still in use: {previous}",
            cfg=cfg,
        )
        return {"fallback": "previous snapshot", "snapshot": str(previous)}

    size = path.stat().st_size
    log.info("Open Food Facts dump ready: %s (%.2f GB, %d rows)", path, size / 1e9, rows)
    return {"path": str(path), "bytes": size, "rows": rows}


def download(cfg: Config, force: bool = False) -> Path:
    """Fetch the dump with retries; raises DataUnavailableError when it can't be had."""
    off = cfg["off"]
    attempts = int(off.get("download_attempts", 3))
    wait_s = float(off.get("retry_wait_s", 30))
    retrying = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=wait_s, max=wait_s * 8),
        retry=retry_if_exception(_retryable),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    source = f"{off['hf_repo']}/{off['hf_file']}"
    log.info("Downloading %s from Hugging Face (resumes; skips if up to date)", source)
    try:
        for attempt in retrying:
            with attempt:
                local = hf_hub_download(
                    repo_id=off["hf_repo"],
                    filename=off["hf_file"],
                    repo_type="dataset",
                    local_dir=cfg.path(off["raw_dir"]),
                    force_download=force,
                )
    except NOT_FOUND as exc:
        raise DataUnavailableError(
            f"{source} not found on Hugging Face ({type(exc).__name__}); "
            "check off.hf_repo and off.hf_file in config/pipeline.yaml"
        ) from exc
    except (OSError, XetDownloadError) as exc:
        raise DataUnavailableError(
            f"Could not download {source} after {attempts} attempts "
            f"({type(exc).__name__}: {exc}); check the network or huggingface.co status"
        ) from exc
    return Path(local)


def check_raw_file(path: Path, min_bytes: int) -> int:
    """Return the row count, or raise DataCorruptError if the file can't be what we need."""
    if not path.is_file():
        raise DataCorruptError(f"{path} is missing after the download")
    size = path.stat().st_size
    if size < min_bytes:
        raise DataCorruptError(
            f"{path} is {size:,} bytes, below off.min_raw_bytes ({min_bytes:,}): partial download?"
        )
    try:
        meta = pq.ParquetFile(path)
    except Exception as exc:  # noqa: BLE001 - pyarrow raises several types for a bad footer
        raise DataCorruptError(f"{path} is not a readable Parquet file ({exc})") from exc
    missing = [c for c in REQUIRED_COLUMNS if c not in meta.schema_arrow.names]
    if missing:
        raise DataCorruptError(
            f"{path} lacks columns {missing}: the Open Food Facts export format changed?"
        )
    return meta.metadata.num_rows


def pull_snapshot_from_dvc(cfg: Config) -> dict:
    """Get the filtered snapshot from the DVC remote instead of building it."""
    target = cfg.path(cfg["off"]["output"])
    pointer = target.with_name(target.name + ".dvc")  # robust target (see versioning.py)
    log.info("off.source is dvc: pulling %s from the DVC remote", target)
    try:
        done = subprocess.run(
            [sys.executable, "-m", "dvc", "pull", str(pointer)],
            cwd=cfg.home, capture_output=True, text=True, timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DataUnavailableError(f"Could not run dvc pull ({exc}); is dvc installed?") from exc
    if done.returncode != 0 or not target.exists():
        tail = (done.stderr or done.stdout).strip()[-500:]
        raise DataUnavailableError(
            f"dvc pull {target.name} failed (exit {done.returncode}): {tail}; "
            "check the DVC remote in .dvc/config, or set off.source to download"
        )
    return {"source": "dvc", "snapshot": str(target), "bytes": target.stat().st_size}
