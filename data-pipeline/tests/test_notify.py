"""The end-of-run summary reads whatever reports exist and never fails."""

import json

import pytest

from pantrypath_pipeline import notify


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_alert",
                        lambda subject, body, level="warning", cfg=None:
                        sent.append((level, subject, body)))
    return sent


def write(cfg, rel, data):
    path = cfg.path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_summary_collects_every_report(cfg, alerts):
    write(cfg, cfg["stats"]["output"], {
        "off": {"rows": 458_663}, "catalog": {"dishes": 100, "cuisine": {"american": 60}},
        "outliers": {"catalog_recipe_count": 4, "off_ingredients_text_length": 10}})
    write(cfg, cfg["drift"]["output"], {"status": "warning", "checks": [
        {"status": "warning"}, {"status": "ok"}]})
    results = cfg["validation"]["results_dir"]
    write(cfg, f"{results}/off_products.json", {"warnings": 1})
    write(cfg, f"{results}/catalog_dishes.json", {"warnings": 2})
    write(cfg, cfg["bias"]["output_json"], {"headline": "South Asian dishes: 2x unknown rate"})
    write(cfg, cfg["llm"]["usage_report"], {"totals": {"calls": 240, "cost_usd": 1.2,
                                                       "model": "x"}})

    summary = notify.run(cfg)
    assert summary["off_rows"] == 458_663 and summary["dishes"] == 100
    assert summary["quality_warnings"] == 3 and summary["outliers"] == 14
    assert summary["drift"] == "warning" and summary["drift_warnings"] == 1
    assert summary["bias"] == "South Asian dishes: 2x unknown rate"
    assert summary["llm_usage"] == {"calls": 240, "cost_usd": 1.2}
    level, subject, body = alerts[0]
    assert level == "info" and subject == "pipeline run succeeded"
    assert "458663" in body and "cost_usd=1.2" in body


def test_summary_without_reports_still_succeeds(cfg, alerts):
    summary = notify.run(cfg)
    assert summary["off_rows"] is None and summary["drift"] == "not run"
    assert summary["bias"] is None and summary["llm_usage"] == {}
    assert alerts[0][0] == "info"


@pytest.mark.parametrize("report, headline", [
    ({"summary": "fine"}, "fine"),
    ({"summary": {"headline": "nested"}}, "nested"),
    ({"slices": []}, None),
    (["not", "a", "dict"], None),
])
def test_bias_headline_shapes(report, headline):
    assert notify.bias_headline(report) == headline
