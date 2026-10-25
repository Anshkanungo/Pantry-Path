"""Drift: did the data change more than expected since the last run?

Compares reports/stats.json with the previous run's copy (stats_prev.json):
- row counts: a change above drift.warn_row_change warns, above fail_row_change fails
  (a 30% drop in Open Food Facts products usually means a broken download or filter);
- rates (missing values, evidence, allergen coverage, popularity tiers, LLM-added
  ingredients): a shift above drift.warn_rate_shift (absolute) warns.
It also checks the catalog's cuisine mix against the quotas, which needs no baseline.
Every comparison is written to reports/drift.json.
"""

import json
import logging
from pathlib import Path
from typing import Any

from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataUnavailableError, ValidationFailedError

log = logging.getLogger(__name__)

OK, WARNING, FAILED = "ok", "warning", "failed"
ROW_METRICS = ["off.rows", "catalog.dishes", "catalog.ingredients_total"]
RATE_GROUPS = ["off.null_rate", "off.evidence_rate", "off.allergen_coverage", "off.scans_tier"]
RATE_METRICS = ["catalog.llm_added_share"]


def flatten(stats: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """{'off': {'rows': 5}} -> {'off.rows': 5}"""
    flat: dict[str, float] = {}
    for key, value in stats.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        elif isinstance(value, int | float) and not isinstance(value, bool):
            flat[name] = value
    return flat


def compare_rows(metric: str, previous: float | None, current: float | None,
                 warn: float, fail: float) -> dict[str, Any]:
    check = {"metric": metric, "kind": "row_count", "previous": previous, "current": current,
             "change": None, "warn_above": warn, "fail_above": fail, "status": OK}
    if current is None:
        check.update(status=WARNING, note="metric missing from this run")
    elif not previous:
        check.update(status=OK if not current else WARNING, note="baseline was empty")
    else:
        change = (current - previous) / previous
        check["change"] = round(change, 6)
        if abs(change) > fail:
            check["status"] = FAILED
        elif abs(change) > warn:
            check["status"] = WARNING
    return check


def compare_rate(metric: str, previous: float, current: float | None,
                 warn: float) -> dict[str, Any]:
    check = {"metric": metric, "kind": "rate", "previous": previous, "current": current,
             "change": None, "warn_above": warn, "status": OK}
    if current is None:
        check.update(status=WARNING, note="metric missing from this run")
    else:
        check["change"] = round(current - previous, 6)
        if abs(check["change"]) > warn:
            check["status"] = WARNING
    return check


def compare_quotas(current: dict[str, float], quotas: dict[str, int]) -> list[dict[str, Any]]:
    """Each cuisine's dish count against its quota (bias guard; no baseline needed)."""
    checks = []
    for cuisine, quota in quotas.items():
        count = current.get(f"catalog.cuisine.{cuisine}", 0)
        checks.append({"metric": f"catalog.cuisine.{cuisine}", "kind": "quota",
                       "expected": quota, "current": count, "change": count - quota,
                       "status": OK if count == quota else WARNING})
    return checks


def compare(current: dict, previous: dict | None, cfg: Config) -> list[dict[str, Any]]:
    settings = cfg["drift"]
    now = flatten(current)
    checks: list[dict[str, Any]] = []
    if previous is not None:
        before = flatten(previous)
        for metric in ROW_METRICS:
            if metric in before:
                checks.append(compare_rows(metric, before[metric], now.get(metric),
                                           settings["warn_row_change"],
                                           settings["fail_row_change"]))
        rates = [m for m in before
                 if m in RATE_METRICS or any(m.startswith(g + ".") for g in RATE_GROUPS)]
        for metric in rates:
            checks.append(compare_rate(metric, before[metric], now.get(metric),
                                       settings["warn_rate_shift"]))
    if "catalog" in current:
        checks += compare_quotas(now, cfg["catalog"]["quotas"])
    return checks


def describe(check: dict[str, Any]) -> str:
    if check["kind"] == "quota":
        return (f"{check['metric']}: {check['current']} dishes, quota {check['expected']}")
    if check.get("note"):
        return f"{check['metric']}: {check['note']} (previous {check['previous']})"
    change = check["change"]
    shown = f"{change:+.1%}" if check["kind"] == "row_count" else f"{change:+.3f}"
    return f"{check['metric']}: {check['previous']} -> {check['current']} ({shown})"


def read_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.is_file() else None


def run(cfg: Config, **_: Any) -> dict[str, Any]:
    """Compare this run's stats with the previous run's; alert on drift, fail on big drops."""
    current_path = cfg.path(cfg["stats"]["output"])
    current = read_json(current_path)
    if current is None:
        raise DataUnavailableError(f"{current_path} not found; run compute-stats first")
    previous = read_json(cfg.path(cfg["stats"]["previous"]))
    if previous is None:
        log.info("No previous stats: first run, so this run becomes the drift baseline")

    checks = compare(current, previous, cfg)
    warnings = [c for c in checks if c["status"] == WARNING]
    failures = [c for c in checks if c["status"] == FAILED]
    status = FAILED if failures else WARNING if warnings else OK
    output = cfg.path(cfg["drift"]["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "status": status,
        "baseline_available": previous is not None,
        "thresholds": {k: cfg["drift"][k]
                       for k in ("warn_row_change", "fail_row_change", "warn_rate_shift")},
        "checks": checks,
    }, indent=2) + "\n")
    log.info("drift: %s (%d checks, %d warnings, %d failures), saved %s",
             status, len(checks), len(warnings), len(failures), output)

    if warnings:
        send_alert(f"data drift: {len(warnings)} warning(s)",
                   "\n".join(describe(c) for c in warnings), level="warning", cfg=cfg)
    if failures:
        lines = [describe(c) for c in failures]
        send_alert(f"data drift: {len(failures)} blocking change(s)", "\n".join(lines),
                   level="error", cfg=cfg)
        error = ValidationFailedError("row counts changed too much: " + "; ".join(lines))
        error.alerted = True  # the detailed alert above was already sent
        raise error
    return {"status": status, "baseline": previous is not None,
            "warnings": len(warnings), "failures": 0}
