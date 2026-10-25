"""versioning.run: dvc add + push in a throwaway git + DVC repo (a local folder is the
remote, so nothing touches the network)."""

import subprocess
import sys

import pytest

from pantrypath_pipeline import versioning
from pantrypath_pipeline.errors import PipelineError


def dvc(*args, cwd):
    subprocess.run([sys.executable, "-m", "dvc", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(versioning, "send_alert", lambda *a, **kw: sent.append(a))
    return sent


@pytest.fixture
def repo_cfg(cfg, monkeypatch):
    """cfg's temp folder as a fresh git + DVC repo, with 2 of the 4 tracked outputs."""
    monkeypatch.setenv("DVC_NO_ANALYTICS", "1")
    subprocess.run(["git", "init", "-q"], cwd=cfg.home, check=True)
    dvc("init", "-q", cwd=cfg.home)
    dvc("config", "core.analytics", "false", cwd=cfg.home)
    dvc("config", "core.check_update", "false", cwd=cfg.home)
    (cfg.home / "dvc.yaml").write_text("metrics:\n  - reports/stats.json\n")  # like data-pipeline/
    (cfg.path("data/processed/dish_profiles.json")).write_text('{"dishes": []}')
    cache = cfg.path("data/processed/llm_cache")
    cache.mkdir()
    (cache / "3f2a.json").write_text('{"reply": "{}"}')
    return cfg


def test_adds_existing_outputs_and_skips_missing(repo_cfg, alerts):
    result = versioning.run(repo_cfg)

    assert result["versioned"] == ["data/processed/llm_cache", "data/processed/dish_profiles.json"]
    assert result["missing"] == ["data/processed/off_products.parquet",
                                 "data/processed/pantrypath.sqlite"]
    assert result["changed_pointers"] == ["data/processed/llm_cache.dvc",
                                          "data/processed/dish_profiles.json.dvc"]
    assert repo_cfg.path("data/processed/llm_cache.dvc").exists()
    assert "/dish_profiles.json" in repo_cfg.path("data/processed/.gitignore").read_text()
    # No remote yet: versioned locally, warned, not failed
    assert result["pushed"] is False and result["remote"] is None
    assert [a[0] for a in alerts] == ["DVC push skipped: no remote"]


def test_unchanged_data_changes_no_pointer_and_edits_do(repo_cfg, alerts):
    versioning.run(repo_cfg)
    assert versioning.run(repo_cfg)["changed_pointers"] == []

    repo_cfg.path("data/processed/dish_profiles.json").write_text('{"dishes": [1]}')
    assert versioning.run(repo_cfg)["changed_pointers"] == ["data/processed/dish_profiles.json.dvc"]


def test_pushes_to_the_configured_remote(repo_cfg, alerts, tmp_path_factory):
    storage = tmp_path_factory.mktemp("remote")
    other = tmp_path_factory.mktemp("other")
    dvc("remote", "add", "-d", "other", str(other), cwd=repo_cfg.home)
    dvc("remote", "add", "pantrypath-gcs", str(storage), cwd=repo_cfg.home)

    result = versioning.run(repo_cfg)

    assert result["pushed"] is True and result["remote"] == "pantrypath-gcs"
    assert any(p.is_file() for p in storage.rglob("*"))
    assert not any(other.rglob("*"))
    assert alerts == []


def test_push_works_when_git_ignores_the_data_folder(repo_cfg, alerts, tmp_path_factory):
    # This .gitignore hides data/processed/ from DVC's repo scan (git un-ignores the
    # .dvc files, DVC's scan doesn't), so a push naming output paths fails with "does
    # not exist as an output or a stage name in 'dvc.yaml'". Naming the .dvc files works.
    (repo_cfg.home / ".gitignore").write_text("data/processed/*\n!data/processed/*.dvc\n")
    storage = tmp_path_factory.mktemp("remote")
    dvc("remote", "add", "-d", "pantrypath-gcs", str(storage), cwd=repo_cfg.home)

    assert versioning.run(repo_cfg)["pushed"] is True
    assert any(p.is_file() for p in storage.rglob("*"))


def test_read_only_clone_skips_the_push_with_a_warning(repo_cfg, alerts, monkeypatch):
    # A grader's clone: our public GCS remote is configured, but no write credentials
    monkeypatch.setattr(versioning, "has_write_credentials", lambda settings, remote: False)
    dvc("remote", "add", "-d", "pantrypath-gcs", "gs://pantry-path-dvc/dvcstore",
        cwd=repo_cfg.home)

    result = versioning.run(repo_cfg)

    assert result["pushed"] is False and result["remote"] == "pantrypath-gcs"
    assert result["changed_pointers"]  # still versioned locally
    assert [a[0] for a in alerts] == ["DVC push skipped: read-only"]


@pytest.mark.parametrize("settings, env, expected", [
    ({"remote.r.url": "/tmp/store"}, {}, True),
    ({"remote.r.url": "gs://b/x"}, {}, False),
    ({"remote.r.url": "gs://b/x", "remote.r.credentialpath": "/k.json"}, {}, True),
    ({"remote.r.url": "gs://b/x"}, {"GOOGLE_APPLICATION_CREDENTIALS": "/k.json"}, True),
])
def test_has_write_credentials(settings, env, expected, monkeypatch, tmp_path):
    monkeypatch.setattr(versioning.Path, "home", lambda: tmp_path)  # no gcloud login here
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert versioning.has_write_credentials(settings, "r") is expected


def test_falls_back_to_the_default_remote(repo_cfg, alerts, tmp_path_factory):
    storage = tmp_path_factory.mktemp("remote")
    dvc("remote", "add", "-d", "teamstore", str(storage), cwd=repo_cfg.home)
    assert versioning.run(repo_cfg)["remote"] == "teamstore"
    assert any(p.is_file() for p in storage.rglob("*"))


def test_push_can_be_switched_off(repo_cfg, alerts):
    repo_cfg.data["dvc"] = {**repo_cfg["dvc"], "push": False}
    result = versioning.run(repo_cfg)
    assert result["pushed"] is False and alerts == []


def test_nothing_to_version_alerts_instead_of_failing(cfg, alerts):
    result = versioning.run(cfg)
    assert result["versioned"] == [] and len(result["missing"]) == 4
    assert [a[0] for a in alerts] == ["DVC: nothing to version"]


def test_dvc_errors_become_pipeline_errors(cfg, alerts, monkeypatch):
    monkeypatch.setenv("DVC_NO_ANALYTICS", "1")
    cfg.path("data/processed/dish_profiles.json").write_text("{}")
    with pytest.raises(PipelineError, match="dvc add"):
        versioning.run(cfg)  # the temp folder is not a DVC repository


def test_pull_llm_cache_restores_the_saved_replies(repo_cfg, alerts, tmp_path_factory):
    storage = tmp_path_factory.mktemp("remote")
    dvc("remote", "add", "-d", "pantrypath-gcs", str(storage), cwd=repo_cfg.home)
    versioning.run(repo_cfg)  # version + push the replies, as the owner's run does
    cache = repo_cfg.path("data/processed/llm_cache")
    for reply in cache.iterdir():
        reply.unlink()
    cache.rmdir()  # a fresh clone: pointer committed, replies not on disk

    result = versioning.run_pull_llm_cache(repo_cfg)

    assert result == {"pulled": True, "saved_replies": 1}
    assert (cache / "3f2a.json").read_text() == '{"reply": "{}"}'


def test_pull_llm_cache_without_a_pointer_does_nothing(repo_cfg, alerts):
    assert versioning.run_pull_llm_cache(repo_cfg) == {
        "pulled": False, "reason": "no pointer file"}


def test_pull_llm_cache_failure_warns_instead_of_failing(repo_cfg, alerts, monkeypatch):
    repo_cfg.path("data/processed/llm_cache.dvc").write_text("outs: []\n")

    def broken(*args, **kwargs):
        raise PipelineError("`dvc pull` failed: network unreachable")

    monkeypatch.setattr(versioning, "dvc", broken)
    result = versioning.run_pull_llm_cache(repo_cfg)

    assert result["pulled"] is False and "network unreachable" in result["reason"]
    assert [a[0] for a in alerts] == ["Saved LLM replies not pulled"]
