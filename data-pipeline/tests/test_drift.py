"""Drift between runs: thresholds, first-run baseline, cuisine quotas."""

import json

import pytest
from fixtures.val_builders import SMALL_QUOTAS, use_small_limits

from pantrypath_pipeline.errors import DataUnavailableError, ValidationFailedError
from pantrypath_pipeline.validate import drift


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(drift, "send_alert",
                        lambda subject, body, level="warning", cfg=None:
                        sent.append((level, subject, body)))
    return sent


@pytest.fixture
def small_cfg(cfg):
    use_small_limits(cfg)
    return cfg


def stats(off_rows=400_000, ingredients_rate=0.9, dishes=4, cuisine=None, llm_share=0.1):
    return {
        "off": {"rows": off_rows, "file_mb": 80.0,
                "null_rate": {"product_name": 0.02},
                "evidence_rate": {"ingredients": ingredients_rate, "allergens": 0.5}},
        "catalog": {"dishes": dishes, "ingredients_total": dishes * 5,
                    "cuisine": cuisine or dict(SMALL_QUOTAS), "llm_added_share": llm_share},
    }


def write(cfg, current, previous=None):
    cfg.path(cfg["stats"]["output"]).write_text(json.dumps(current))
    if previous is not None:
        cfg.path(cfg["stats"]["previous"]).write_text(json.dumps(previous))


def report(cfg):
    return json.loads(cfg.path(cfg["drift"]["output"]).read_text())


def check(cfg, metric):
    return next(c for c in report(cfg)["checks"] if c["metric"] == metric)


def test_first_run_has_no_baseline(small_cfg, alerts):
    write(small_cfg, stats())
    assert drift.run(small_cfg) == {"status": "ok", "baseline": False, "warnings": 0,
                                    "failures": 0}
    assert report(small_cfg)["baseline_available"] is False
    assert all(c["kind"] == "quota" for c in report(small_cfg)["checks"])
    assert alerts == []


def test_small_changes_are_ok(small_cfg, alerts):
    write(small_cfg, stats(off_rows=420_000, ingredients_rate=0.92), stats())
    assert drift.run(small_cfg)["status"] == "ok"
    assert check(small_cfg, "off.rows")["change"] == pytest.approx(0.05)
    assert alerts == []


def test_row_change_above_warn_threshold_warns(small_cfg, alerts):
    write(small_cfg, stats(off_rows=340_000), stats())  # -15%
    result = drift.run(small_cfg)
    assert result["status"] == "warning" and result["warnings"] == 1
    assert check(small_cfg, "off.rows")["status"] == "warning"
    assert alerts[0][0] == "warning" and "off.rows" in alerts[0][2]


def test_row_drop_above_fail_threshold_fails(small_cfg, alerts):
    write(small_cfg, stats(off_rows=200_000), stats())  # -50%
    with pytest.raises(ValidationFailedError, match="off.rows") as err:
        drift.run(small_cfg)
    assert err.value.alerted is True
    assert report(small_cfg)["status"] == "failed"
    assert [a[0] for a in alerts] == ["error"]


def test_rate_shift_warns(small_cfg, alerts):
    write(small_cfg, stats(ingredients_rate=0.80), stats(ingredients_rate=0.90))
    assert drift.run(small_cfg)["status"] == "warning"
    shifted = check(small_cfg, "off.evidence_rate.ingredients")
    assert shifted["status"] == "warning" and shifted["change"] == pytest.approx(-0.10)
    assert check(small_cfg, "catalog.llm_added_share")["status"] == "ok"


def test_cuisine_mix_off_quota_warns_without_baseline(small_cfg, alerts):
    write(small_cfg, stats(cuisine={**SMALL_QUOTAS, "american": 1, "east_se_asian": 1}))
    result = drift.run(small_cfg)
    assert result["status"] == "warning" and result["warnings"] == 2
    assert check(small_cfg, "catalog.cuisine.east_se_asian")["change"] == 1


def test_metric_missing_from_this_run_warns(small_cfg, alerts):
    current = stats()
    del current["catalog"]
    write(small_cfg, current, stats())
    assert drift.run(small_cfg)["status"] == "warning"
    assert check(small_cfg, "catalog.dishes")["note"] == "metric missing from this run"


def test_missing_stats_is_unavailable(small_cfg):
    with pytest.raises(DataUnavailableError, match="compute-stats"):
        drift.run(small_cfg)


def test_flatten():
    assert drift.flatten({"off": {"rows": 5, "null_rate": {"brands": 0.1}}, "x": "text"}) == {
        "off.rows": 5, "off.null_rate.brands": 0.1}
