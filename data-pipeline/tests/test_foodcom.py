"""Food.com download step: skip valid files, replace broken ones, fail clearly."""

import os
import subprocess
import zipfile

import pandas as pd
import pytest

from pantrypath_pipeline.acquire import foodcom
from pantrypath_pipeline.errors import DataCorruptError, DataUnavailableError


def write_recipes(path, rows=3, drop=()):
    """A tiny Food.com-shaped parquet file."""
    df = pd.DataFrame({
        "RecipeId": [float(i) for i in range(rows)],
        "Name": [f"Recipe {i}" for i in range(rows)],
        "RecipeIngredientParts": [["eggs", "milk"]] * rows,
        "Keywords": [["Easy"]] * rows,
        "RecipeCategory": ["Breakfast"] * rows,
        "ReviewCount": [1.0] * rows,
        "AggregatedRating": [4.5] * rows,
    })
    df = df.drop(columns=list(drop))
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return path


@pytest.fixture
def small_cfg(cfg):
    cfg["foodcom"]["min_raw_rows"] = 3
    return cfg


def test_check_file_accepts_a_valid_file(tmp_path):
    assert foodcom.check_file(write_recipes(tmp_path / "r.parquet"), min_rows=3) == 3


@pytest.mark.parametrize("make, message", [
    (lambda p: p.write_bytes(b"not parquet"), "not a readable Parquet"),
    (lambda p: write_recipes(p, drop=["Keywords"]), "lacks columns"),
    (lambda p: write_recipes(p, rows=2), "expected at least 3"),
])
def test_check_file_rejects_broken_files(tmp_path, make, message):
    path = tmp_path / "r.parquet"
    make(path)
    with pytest.raises(DataCorruptError, match=message):
        foodcom.check_file(path, min_rows=3)


def test_run_skips_download_when_file_is_valid(small_cfg, monkeypatch):
    write_recipes(foodcom.raw_path(small_cfg))
    monkeypatch.setattr(foodcom, "download", lambda *a, **k: pytest.fail("should not download"))
    assert foodcom.run(small_cfg)["downloaded"] is False


def test_run_replaces_a_corrupt_file(small_cfg, monkeypatch):
    path = foodcom.raw_path(small_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"truncated")
    calls = []

    def fake_download(dataset, file_name, dest_dir, **kwargs):
        calls.append((dataset, file_name))
        return write_recipes(dest_dir / file_name)

    monkeypatch.setattr(foodcom, "download", fake_download)
    summary = foodcom.run(small_cfg)
    assert summary == {"rows": 3, "downloaded": True, "bytes": path.stat().st_size}
    assert calls == [("irkaal/foodcom-recipes-and-reviews", "recipes.parquet")]


def test_run_retries_once_when_the_download_is_broken(small_cfg, monkeypatch):
    attempts = []

    def flaky_download(dataset, file_name, dest_dir, **kwargs):
        attempts.append(1)
        target = dest_dir / file_name
        if len(attempts) == 1:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"half a file")
            return target
        return write_recipes(target)

    monkeypatch.setattr(foodcom, "download", flaky_download)
    assert foodcom.run(small_cfg)["rows"] == 3
    assert len(attempts) == 2


def test_run_fails_when_the_download_stays_broken(small_cfg, monkeypatch):
    def broken_download(dataset, file_name, dest_dir, **kwargs):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / file_name).write_bytes(b"junk")

    monkeypatch.setattr(foodcom, "download", broken_download)
    with pytest.raises(DataCorruptError):
        foodcom.run(small_cfg)


def test_download_retries_then_raises_data_unavailable(tmp_path, monkeypatch):
    calls = []

    def failing_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="404 Not Found")

    monkeypatch.setattr(foodcom.subprocess, "run", failing_run)
    with pytest.raises(DataUnavailableError, match="404 Not Found"):
        foodcom.download("owner/data", "recipes.parquet", tmp_path, attempts=3, wait_s=0)
    assert len(calls) == 3
    assert calls[0][1:4] == ["-m", "kaggle", "datasets"]  # the CLI allows anonymous downloads


def test_download_unzips_kaggle_archives(tmp_path, monkeypatch):
    source = write_recipes(tmp_path / "src" / "recipes.parquet")

    def zipping_run(cmd, **kwargs):
        dest = tmp_path / "dest"
        with zipfile.ZipFile(dest / "recipes.parquet.zip", "w") as zf:
            zf.write(source, "recipes.parquet")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(foodcom.subprocess, "run", zipping_run)
    path = foodcom.download("owner/data", "recipes.parquet", tmp_path / "dest", wait_s=0)
    assert foodcom.check_file(path, min_rows=3) == 3
    assert not (tmp_path / "dest" / "recipes.parquet.zip").exists()


def test_download_rejects_a_bad_zip(tmp_path, monkeypatch):
    def bad_zip_run(cmd, **kwargs):
        (tmp_path / "recipes.parquet.zip").write_bytes(b"not a zip")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(foodcom.subprocess, "run", bad_zip_run)
    with pytest.raises(DataCorruptError, match="not a valid zip"):
        foodcom.download("owner/data", "recipes.parquet", tmp_path, wait_s=0)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
def test_live_anonymous_download(tmp_path):
    path = foodcom.download("irkaal/foodcom-recipes-and-reviews", "recipes.parquet", tmp_path)
    assert foodcom.check_file(path, min_rows=400_000) > 400_000
