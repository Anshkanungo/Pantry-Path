"""Version the pipeline's outputs with DVC: `dvc add` each tracked path, then `dvc push`.

This is the batch DAG's last data step. It never commits: `dvc add` writes a small .dvc
pointer file next to each output, and a person reviews and commits those with git, so
every data version is tied to a code version. Without a DVC remote the data is still
versioned in the local DVC cache; the step warns (and alerts) instead of failing.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import PipelineError

log = logging.getLogger(__name__)

# This interpreter's DVC, so it works even when the venv isn't on PATH (as under Airflow)
DVC = [sys.executable, "-m", "dvc"]


def dvc(*args: str, cwd: Path, timeout: int = 3600,
        check: bool = True) -> subprocess.CompletedProcess:
    """Run one dvc command. With check, a non-zero exit raises PipelineError with DVC's output."""
    command = " ".join(["dvc", *args])
    try:
        proc = subprocess.run([*DVC, *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"`{command}` timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        output = (proc.stderr.strip() or proc.stdout.strip())[-2000:]
        raise PipelineError(f"`{command}` failed (exit {proc.returncode}) in {cwd}: {output}")
    return proc


def dvc_settings(cwd: Path) -> dict[str, str]:
    """`dvc config --list` as a dict: the repo's config merged with the git-ignored
    config.local (where credentials and personal remotes live)."""
    return dict(
        line.split("=", 1) for line in dvc("config", "--list", cwd=cwd).stdout.splitlines()
        if "=" in line
    )


def has_write_credentials(settings: dict[str, str], remote: str) -> bool:
    """Whether a push to a GCS remote can authenticate. Anyone can pull from our
    public bucket, but only the service account can push; graders' clones can't."""
    if not settings.get(f"remote.{remote}.url", "").startswith("gs://"):
        return True  # local folders and other remotes: let dvc decide
    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    return bool(settings.get(f"remote.{remote}.credentialpath")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or adc.is_file())


def find_remote(cwd: Path, preferred: str | None) -> str | None:
    """The remote to push to: `preferred` if it is configured, else the default, else None."""
    settings = dvc_settings(cwd)
    remotes = {key[len("remote."):-len(".url")] for key in settings
               if key.startswith("remote.") and key.endswith(".url")}
    if preferred in remotes:
        return preferred
    default = settings.get("core.remote")
    return default if default in remotes else None


def pointer(cfg: Config, rel: str) -> Path:
    """The .dvc file `dvc add` writes for an output (works for files and folders)."""
    path = cfg.path(rel)
    return path.with_name(path.name + ".dvc")


def _read(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


def run(cfg: Config, **_: Any) -> dict:
    settings = cfg["dvc"]
    tracked = list(settings["tracked"])
    targets = [rel for rel in tracked if cfg.path(rel).exists()]
    missing = [rel for rel in tracked if rel not in targets]
    for rel in missing:
        log.warning("Not versioned: %s doesn't exist yet (did its step run?)", rel)
    if not targets:
        send_alert("DVC: nothing to version",
                   f"None of {tracked} exist under {cfg.home}", level="warning", cfg=cfg)
        return {"versioned": [], "missing": missing, "changed_pointers": [],
                "remote": None, "pushed": False}

    before = {rel: _read(pointer(cfg, rel)) for rel in targets}
    dvc("add", *targets, cwd=cfg.home)
    changed = [rel + ".dvc" for rel in targets if _read(pointer(cfg, rel)) != before[rel]]
    if changed:
        log.info("New data version. Review, then commit from data-pipeline/: git add %s %s",
                 " ".join(changed), " ".join(sorted({str(Path(c).parent / ".gitignore")
                                                    for c in changed})))
    else:
        log.info("Data unchanged since the last version; no pointer files changed")

    remote, pushed = None, False
    if settings.get("push", True):
        remote = find_remote(cfg.home, settings.get("remote"))
        if remote and not has_write_credentials(dvc_settings(cfg.home), remote):
            message = (
                f"No write credentials for DVC remote {remote!r} (a read-only clone), so the "
                "data is versioned in the local DVC cache only. Owners push with: "
                f"dvc remote modify --local {remote} credentialpath <service-account-key.json>"
            )
            log.warning(message)
            send_alert("DVC push skipped: read-only", message, level="warning", cfg=cfg)
        elif remote:
            # Push by .dvc file, not by output path: DVC only resolves an output path if
            # its scan of the repo finds the .dvc file, and the scan skips git-ignored
            # folders ("does not exist as an output or a stage name in 'dvc.yaml'").
            pointers = [str(pointer(cfg, rel).relative_to(cfg.home)) for rel in targets]
            dvc("push", "-r", remote, *pointers, cwd=cfg.home)
            pushed = True
            log.info("Pushed %d outputs to DVC remote %r", len(targets), remote)
        else:
            message = (
                "No DVC remote is configured, so the data is versioned in the local DVC cache "
                "only. Add one from the repo root: "
                f"dvc remote add -d {settings.get('remote') or 'storage'} gs://<bucket>/dvcstore"
            )
            log.warning(message)
            send_alert("DVC push skipped: no remote", message, level="warning", cfg=cfg)
    return {"versioned": targets, "missing": missing, "changed_pointers": changed,
            "remote": remote, "pushed": pushed}
