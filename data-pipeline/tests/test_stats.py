"""Statistics and IQR outliers for the OFF snapshot and the dish catalog."""

import json

import pytest
from fixtures.val_builders import (
    dish,
    off_row,
    off_rows,
    small_catalog,
    use_small_limits,
    write_catalog,
    write_off,
)

from pantrypath_pipeline.errors import DataUnavailableError
from pantrypath_pipeline.validate import stats


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(stats, "send_alert",
                        lambda subject, body, level="warning", cfg=None:
                        sent.append((level, subject, body)))
    return sent


@pytest.fixture
def small_cfg(cfg):
    use_small_limits(cfg)
    return cfg


def read(cfg, key):
    return json.loads(cfg.path(cfg["stats"][key]).read_text())


def test_iqr_flags_values_far_from_the_middle():
    values = {"a": 10, "b": 11, "c": 12, "d": 13, "e": 14, "f": 90}
    found = stats.find_outliers(values, k=1.5, top=5)
    assert found["count"] == 1 and found["examples"] == [{"id": "f", "value": 90}]
    assert found["low"] < 10 and 14 < found["high"] < 90


def test_iqr_needs_at_least_four_values():
    assert stats.find_outliers({"a": 1, "b": 2, "c": 500}, k=1.5, top=5)["count"] == 0


def test_log_scale_tolerates_a_long_right_tail():
    # Recipes per dish: popular dishes have many more, which is normal
    counts = {"a": 20, "b": 30, "c": 45, "d": 60, "e": 90, "f": 150, "g": 400}
    assert stats.find_outliers(counts, k=1.5, top=5)["count"] == 1  # plain rule flags g
    assert stats.find_outliers(counts, k=1.5, top=5, log_scale=True)["count"] == 0
    extreme = stats.find_outliers({**counts, "h": 50_000}, k=1.5, top=5, log_scale=True)
    assert [e["id"] for e in extreme["examples"]] == ["h"]


def test_upper_only_ignores_small_values():
    lengths = {"water": 5, "a": 200, "b": 210, "c": 220, "d": 230, "e": 240}
    assert stats.find_outliers(lengths, k=1.5, top=5)["count"] == 1
    found = stats.find_outliers(lengths, k=1.5, top=5, upper_only=True)
    assert found["count"] == 0 and found["low"] is None


def test_tier_names_follow_the_bins():
    assert stats.tier_names([0, 1, 10]) == [("0", 0, 1), ("1_9", 1, 10), ("10_plus", 10, None)]


def test_numeric_only_drops_text_and_lists():
    assert stats.numeric_only({"a": 1, "b": "x", "c": [1], "d": {"e": True, "f": None}}) == {
        "a": 1, "d": {"e": 1}}


def test_off_stats(small_cfg):
    rows = off_rows(4) + [
        off_row("0000000000098", product_name=None, allergens_tags=[], unique_scans_n=0,
                ingredients_text="sugar"),
        off_row("0000000000099", ingredients_text="", allergens_tags=["en:peanuts", "en:milk"],
                unique_scans_n=25),
    ]
    path = write_off(small_cfg.path(small_cfg["off"]["output"]), rows)
    result, outliers = stats.off_stats(path, small_cfg)
    assert result["rows"] == 6
    assert result["null_rate"]["product_name"] == pytest.approx(1 / 6, abs=1e-6)
    assert result["null_rate"]["barcode"] == 0
    assert result["evidence_rate"] == pytest.approx(
        {"ingredients": 5 / 6, "allergens": 5 / 6, "both": 4 / 6, "either": 1.0}, abs=1e-6)
    assert result["allergen_coverage"]["milk"] == pytest.approx(5 / 6, abs=1e-6)
    assert result["allergen_coverage"]["peanuts"] == pytest.approx(1 / 6, abs=1e-6)
    assert result["scans_tier"] == pytest.approx(
        {"0": 1 / 6, "1_9": 4 / 6, "10_plus": 1 / 6}, abs=1e-6)
    assert result["ingredients_text_length"]["min"] == len("sugar")
    assert "off_ingredients_text_length" in outliers


def test_long_ingredient_statement_is_an_outlier(small_cfg):
    rows = off_rows(8) + [off_row("0000000000099", ingredients_text="x" * 5000)]
    path = write_off(small_cfg.path(small_cfg["off"]["output"]), rows)
    _, outliers = stats.off_stats(path, small_cfg)
    found = outliers["off_ingredients_text_length"]
    assert found["count"] == 1
    assert found["examples"][0]["id"] == "0000000000099"
    assert found["examples"][0]["value"] == 5000


def test_catalog_stats(small_cfg):
    dishes = small_catalog() + [dish("baked-ziti", n_ingredients=5, llm_added=1)]
    path = write_catalog(small_cfg.path(small_cfg["catalog"]["profiles_output"]), dishes)
    result, outliers = stats.catalog_stats(path, small_cfg)
    assert result["dishes"] == 5
    assert result["cuisine"]["american"] == 3 and result["cuisine"]["latin_american"] == 0
    assert result["ingredients_total"] == 25
    assert result["core"] == 5 and result["common"] == 20
    assert result["llm_added_share"] == pytest.approx(1 / 25)
    assert result["ingredients_per_dish"] == {"min": 5, "median": 5, "max": 5, "mean": 5.0}
    assert set(outliers) == {"catalog_ingredients_per_dish", "catalog_recipe_count"}


def test_dish_with_far_too_many_ingredients_is_an_outlier(small_cfg):
    dishes = small_catalog() + [dish("kitchen-sink-stew", n_ingredients=45)]
    path = write_catalog(small_cfg.path(small_cfg["catalog"]["profiles_output"]), dishes)
    _, outliers = stats.catalog_stats(path, small_cfg)
    found = outliers["catalog_ingredients_per_dish"]
    assert found["count"] == 1 and found["examples"][0]["id"] == "kitchen-sink-stew"


def test_run_writes_numeric_metrics_and_outlier_examples(small_cfg, alerts):
    write_off(small_cfg.path(small_cfg["off"]["output"]),
              off_rows(8) + [off_row("0000000000099", ingredients_text="x" * 5000)])
    write_catalog(small_cfg.path(small_cfg["catalog"]["profiles_output"]), small_catalog())
    summary = stats.run(small_cfg)

    metrics = read(small_cfg, "output")
    assert summary == {"off_rows": 9, "dishes": 4, "outliers": metrics["outliers"]}
    assert_only_numbers(metrics)  # what `dvc metrics diff` can compare
    examples = read(small_cfg, "outliers_output")["off_ingredients_text_length"]["examples"]
    assert examples[0]["id"] == "0000000000099"
    assert [a[0] for a in alerts] == ["warning"]


def assert_only_numbers(value):
    if isinstance(value, dict):
        for item in value.values():
            assert_only_numbers(item)
    else:
        assert isinstance(value, int | float), value


def test_previous_stats_become_the_drift_baseline(small_cfg, alerts):
    off_path = small_cfg.path(small_cfg["off"]["output"])
    write_off(off_path, off_rows(5))
    stats.run(small_cfg)
    assert not small_cfg.path(small_cfg["stats"]["previous"]).exists()

    write_off(off_path, off_rows(7))  # new data: last run's stats become the baseline
    stats.run(small_cfg)
    assert read(small_cfg, "previous")["off"]["rows"] == 5
    assert read(small_cfg, "output")["off"]["rows"] == 7

    stats.run(small_cfg)  # rerun on the same data keeps the baseline
    assert read(small_cfg, "previous")["off"]["rows"] == 5


def test_run_with_only_the_catalog(small_cfg, alerts):
    write_catalog(small_cfg.path(small_cfg["catalog"]["profiles_output"]), small_catalog())
    summary = stats.run(small_cfg)
    assert summary["off_rows"] is None and summary["dishes"] == 4
    assert "off" not in read(small_cfg, "output")


def test_run_includes_numeric_catalog_build_summary(small_cfg, alerts):
    write_catalog(small_cfg.path(small_cfg["catalog"]["profiles_output"]), small_catalog())
    summary_path = small_cfg.path(small_cfg["paths"]["reports"]) / "catalog_summary.json"
    summary_path.write_text(json.dumps({"dropped": 12, "merged": 3, "notes": "text"}))
    stats.run(small_cfg)
    assert read(small_cfg, "output")["catalog_build"] == {"dropped": 12, "merged": 3}


def test_run_without_any_input_is_unavailable(small_cfg):
    with pytest.raises(DataUnavailableError):
        stats.run(small_cfg)
