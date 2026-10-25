"""Great Expectations suites: clean data passes, each kind of bad data is caught."""

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

from pantrypath_core.catalog import Catalog
from pantrypath_pipeline.errors import (
    DataCorruptError,
    DataUnavailableError,
    ValidationFailedError,
)
from pantrypath_pipeline.validate import expectations


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(expectations, "send_alert",
                        lambda subject, body, level="warning", cfg=None:
                        sent.append((level, subject, body)))
    return sent


@pytest.fixture
def small_cfg(cfg):
    use_small_limits(cfg)
    return cfg


def off_path(cfg):
    return cfg.path(cfg["off"]["output"])


def catalog_path(cfg):
    return cfg.path(cfg["catalog"]["profiles_output"])


def saved(cfg, suite):
    return json.loads((cfg.path(cfg["validation"]["results_dir"]) / f"{suite}.json").read_text())


def failed_checks(cfg, suite):
    return [(c["expectation"], c["column"]) for c in saved(cfg, suite)["checks"]
            if not c["success"]]


# --- Open Food Facts --------------------------------------------------------------

def test_clean_off_snapshot_passes(small_cfg, alerts):
    write_off(off_path(small_cfg), off_rows(5))
    summary = expectations.run_off(small_cfg)
    assert summary["critical_failures"] == 0 and summary["warnings"] == 0
    assert summary["suites"] == ["off_schema", "off_products", "off_allergen_tags"]
    assert saved(small_cfg, "off_products")["success"] is True
    assert alerts == []


@pytest.mark.parametrize("bad_row, expected", [
    (off_row("123"), ("expect_column_values_to_match_regex", "barcode")),
    (off_row("12345678901ab"), ("expect_column_values_to_match_regex", "barcode")),
    (off_row(None), ("expect_column_values_to_not_be_null", "barcode")),
    (off_row("0000000000001"), ("expect_column_values_to_be_unique", "barcode")),
    (off_row("0000000000099", ingredients_text="", allergens_tags=[]),
     ("expect_column_values_to_be_in_set", "has_evidence")),
    (off_row("0000000000099", ingredients_text=None, allergens_tags=None),
     ("expect_column_values_to_be_in_set", "has_evidence")),
])
def test_bad_products_block_the_run(small_cfg, alerts, bad_row, expected):
    write_off(off_path(small_cfg), off_rows(4) + [bad_row])
    with pytest.raises(ValidationFailedError) as err:
        expectations.run_off(small_cfg)
    assert err.value.alerted is True
    assert expected in failed_checks(small_cfg, "off_products")
    assert [a[0] for a in alerts] == ["error"]


def test_row_count_outside_expected_range_blocks(small_cfg, alerts):
    small_cfg.data["validation"]["off_expected_rows"] = [10, 100]
    write_off(off_path(small_cfg), off_rows(5))
    with pytest.raises(ValidationFailedError, match="expect_table_row_count_to_be_between"):
        expectations.run_off(small_cfg)


def test_missing_column_is_a_schema_failure(small_cfg, alerts):
    write_off(off_path(small_cfg), off_rows(5), drop=("labels_tags",))
    with pytest.raises(ValidationFailedError, match="off_schema"):
        expectations.run_off(small_cfg)
    report = saved(small_cfg, "off_schema")
    assert report["checks"][0]["expectation"] == "expect_table_columns_to_match_set"


def test_malformed_allergen_tags_block(small_cfg, alerts):
    rows = off_rows(4) + [off_row("0000000000099", allergens_tags=["milk", "Peanuts"])]
    write_off(off_path(small_cfg), rows)
    with pytest.raises(ValidationFailedError):
        expectations.run_off(small_cfg)
    assert ("expect_column_values_to_match_regex", "tag") in failed_checks(
        small_cfg, "off_allergen_tags")


def test_warning_level_problems_alert_but_do_not_block(small_cfg, alerts):
    rows = off_rows(4) + [off_row("0000000000099", unique_scans_n=None, completeness=1.7)]
    write_off(off_path(small_cfg), rows)
    summary = expectations.run_off(small_cfg)
    assert summary["warnings"] == 2 and summary["critical_failures"] == 0
    assert [a[0] for a in alerts] == ["warning"]
    assert "completeness" in alerts[0][2]


def test_missing_snapshot_is_reported_as_unavailable(small_cfg):
    with pytest.raises(DataUnavailableError, match="filter-off"):
        expectations.run_off(small_cfg)


def test_unreadable_snapshot_is_reported_as_corrupt(small_cfg):
    path = off_path(small_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file")
    with pytest.raises(DataCorruptError):
        expectations.run_off(small_cfg)


# --- Dish catalog -------------------------------------------------------------------

def test_fixture_catalog_matches_the_shared_model():
    Catalog.model_validate({"catalog_version": "test", "generated_at": "2026-10-24T00:00:00Z",
                            "dishes": small_catalog()})


def test_clean_catalog_passes(small_cfg, alerts):
    write_catalog(catalog_path(small_cfg), small_catalog())
    summary = expectations.run_catalog(small_cfg)
    assert summary["critical_failures"] == 0 and summary["warnings"] == 0
    assert summary["rows"] == {"catalog_dishes": 4, "catalog_ingredients": 20}
    assert alerts == []


def test_too_few_dishes_blocks(small_cfg, alerts):
    write_catalog(catalog_path(small_cfg), small_catalog()[:2])
    with pytest.raises(ValidationFailedError, match="row_count"):
        expectations.run_catalog(small_cfg)


@pytest.mark.parametrize("bad_dish, expected", [
    (dish("lasagna"), ("expect_column_values_to_be_unique", "dish_id")),
    (dish("baked-ziti", name="lasagna"), ("expect_column_values_to_be_unique", "name")),
    (dish("Baked Ziti"), ("expect_column_values_to_match_regex", "dish_id")),
    (dish("baked-ziti", cuisine="martian"), ("expect_column_values_to_be_in_set", "cuisine")),
    (dish("baked-ziti", n_ingredients=2), ("expect_column_values_to_be_between",
                                           "n_ingredients")),
])
def test_bad_dishes_block(small_cfg, alerts, bad_dish, expected):
    write_catalog(catalog_path(small_cfg), small_catalog() + [bad_dish])
    with pytest.raises(ValidationFailedError):
        expectations.run_catalog(small_cfg)
    assert expected in failed_checks(small_cfg, "catalog_dishes")


def test_dish_without_core_ingredient_blocks(small_cfg, alerts):
    no_core = dish("baked-ziti")
    for ingredient in no_core["ingredients"]:
        ingredient["role"] = "common"
    write_catalog(catalog_path(small_cfg), small_catalog() + [no_core])
    with pytest.raises(ValidationFailedError):
        expectations.run_catalog(small_cfg)
    assert ("expect_column_values_to_be_between", "n_core") in failed_checks(
        small_cfg, "catalog_dishes")


@pytest.mark.parametrize("field, value, expected", [
    ("category", "spaceship", ("expect_column_values_to_be_in_set", "category")),
    ("role", "garnish", ("expect_column_values_to_be_in_set", "role")),
    ("share", 1.5, ("expect_column_values_to_be_between", "share")),
    ("share", None, ("expect_column_values_to_not_be_null", "share")),
    ("name", "ingredient 1", ("expect_compound_columns_to_be_unique", ["dish_id", "name"])),
])
def test_bad_ingredients_block(small_cfg, alerts, field, value, expected):
    dishes = small_catalog()
    dishes[0]["ingredients"][2][field] = value
    write_catalog(catalog_path(small_cfg), dishes)
    with pytest.raises(ValidationFailedError):
        expectations.run_catalog(small_cfg)
    assert expected in failed_checks(small_cfg, "catalog_ingredients")


def test_llm_heavy_dish_only_warns(small_cfg, alerts):
    dishes = small_catalog()
    dishes[0] = dish("lasagna", n_ingredients=5, llm_added=3)  # 60% added by the LLM
    write_catalog(catalog_path(small_cfg), dishes)
    summary = expectations.run_catalog(small_cfg)
    assert summary["warnings"] == 1
    assert alerts[0][0] == "warning" and "llm_added_share" in alerts[0][2]


def test_missing_catalog_is_reported_as_unavailable(small_cfg):
    with pytest.raises(DataUnavailableError, match="assemble-catalog"):
        expectations.run_catalog(small_cfg)


def test_catalog_that_is_not_json_is_corrupt(small_cfg):
    path = catalog_path(small_cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(DataCorruptError):
        expectations.run_catalog(small_cfg)
