"""Download the Food.com recipes (Kaggle dataset irkaal/foodcom-recipes-and-reviews).

The dataset is public and downloads without a Kaggle account, but only through the
kaggle CLI: when the library is imported by another program it refuses anonymous calls
(and exits the process). So we run `python -m kaggle` in a subprocess. If you have a
Kaggle token (KAGGLE_API_TOKEN or ~/.kaggle/access_token) the CLI uses it.

A file that is already there and valid is not downloaded again. A broken file is
deleted and downloaded once more before the step fails.
"""

import logging
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = (
    "RecipeId", "Name", "RecipeIngredientParts", "Keywords", "RecipeCategory",
    "ReviewCount", "AggregatedRating",
)


def raw_path(cfg: Config) -> Path:
    fc = cfg["foodcom"]
    return cfg.path(fc["raw_dir"]) / fc["kaggle_file"]


def check_file(path: Path, min_rows: int) -> int:
    """Return the row count; raise DataCorruptError if unreadable, incomplete or too small."""
    try:
        parquet = pq.ParquetFile(path)
    except Exception as exc:
        raise DataCorruptError(
            f"{path} is not a readable Parquet file ({exc}); delete it and rerun"
        ) from exc
    missing = [c for c in REQUIRED_COLUMNS if c not in parquet.schema_arrow.names]
    if missing:
        raise DataCorruptError(f"{path} lacks columns {missing}; has the Kaggle dataset changed?")
    rows = parquet.metadata.num_rows
    if rows < min_rows:
        raise DataCorruptError(
            f"{path} has {rows} rows, expected at least {min_rows}; the download may be cut short"
        )
    return rows


def _unzip_if_needed(dest_dir: Path, file_name: str) -> None:
    """Kaggle wraps large files in a zip; extract it and delete the archive."""
    archive = dest_dir / f"{file_name}.zip"
    if not archive.exists():
        return
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if Path(m).name == file_name] or zf.namelist()
            zf.extract(members[0], dest_dir)
            extracted = dest_dir / members[0]
            if extracted != dest_dir / file_name:
                extracted.replace(dest_dir / file_name)
    except zipfile.BadZipFile as exc:
        raise DataCorruptError(f"{archive} is not a valid zip ({exc}); rerun the step") from exc
    finally:
        archive.unlink(missing_ok=True)


def download(
    dataset: str,
    file_name: str,
    dest_dir: Path,
    attempts: int = 3,
    timeout_s: int = 1800,
    wait_s: float = 5.0,
) -> Path:
    """Download one file of a Kaggle dataset with the kaggle CLI, retrying with backoff."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "kaggle", "datasets", "download", dataset,
           "-f", file_name, "-p", str(dest_dir), "--force", "--quiet"]
    retrying = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=wait_s, max=120),
        retry=retry_if_exception_type(DataUnavailableError),
        reraise=True,
    )
    for attempt in retrying:
        with attempt:
            n = attempt.retry_state.attempt_number
            log.info("downloading %s/%s (attempt %d of %d)", dataset, file_name, n, attempts)
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise DataUnavailableError(
                    f"Kaggle download of {dataset}/{file_name} took over {timeout_s}s"
                ) from exc
            if proc.returncode != 0:
                tail = " | ".join((proc.stderr or proc.stdout).strip().splitlines()[-3:])
                raise DataUnavailableError(
                    f"Kaggle download of {dataset}/{file_name} failed (exit {proc.returncode}):"
                    f" {tail}. Check the network and https://www.kaggle.com/datasets/{dataset}"
                )
    _unzip_if_needed(dest_dir, file_name)
    target = dest_dir / file_name
    if not target.exists():
        raise DataUnavailableError(f"Kaggle reported success but {target} is missing")
    return target


def run(cfg: Config, **_: Any) -> dict:
    fc = cfg["foodcom"]
    path = raw_path(cfg)
    min_rows = int(fc.get("min_raw_rows", 1))

    if path.exists():
        try:
            rows = check_file(path, min_rows)
            log.info("%s already present (%d rows); skipping download", path, rows)
            return {"rows": rows, "downloaded": False, "bytes": path.stat().st_size}
        except DataCorruptError as exc:
            log.warning("existing file is unusable (%s); downloading again", exc)
            path.unlink()

    def fetch() -> int:
        download(fc["kaggle_dataset"], fc["kaggle_file"], path.parent,
                 attempts=int(fc.get("download_attempts", 3)),
                 timeout_s=int(fc.get("download_timeout_s", 1800)))
        return check_file(path, min_rows)

    try:
        rows = fetch()
    except DataCorruptError as exc:
        log.warning("downloaded file is unusable (%s); deleting it and trying once more", exc)
        path.unlink(missing_ok=True)
        rows = fetch()
    log.info("downloaded %s (%d rows)", path, rows)
    return {"rows": rows, "downloaded": True, "bytes": path.stat().st_size}
