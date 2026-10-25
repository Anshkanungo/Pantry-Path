"""Barcode normalization: every source's code must land on the same GTIN-13."""

import pytest

from pantrypath_core.barcodes import (
    gtin_check_digit,
    kroger_upc_to_gtin13,
    to_gtin13,
    upce_to_upca,
)


@pytest.mark.parametrize("body, check", [
    ("400638133393", "1"),   # EAN-13 4006381333931
    ("03600029145", "2"),    # UPC-A 036000291452
    ("9638507", "4"),        # EAN-8 96385074
    ("001111060903", "8"),   # Kroger Large Eggs, GTIN-13 0011110609038
])
def test_check_digit_on_published_examples(body, check):
    assert gtin_check_digit(body) == check


def test_check_digit_ignores_leading_zeros():
    assert gtin_check_digit("03600029145") == gtin_check_digit("003600029145")


@pytest.mark.parametrize("bad", ["", "12a4"])
def test_check_digit_rejects_non_digits(bad):
    with pytest.raises(ValueError):
        gtin_check_digit(bad)


@pytest.mark.parametrize("code, expected", [
    ("4006381333931", "4006381333931"),    # EAN-13 as is
    ("036000291452", "0036000291452"),     # UPC-A gets a leading 0
    (" 036000291452 ", "0036000291452"),   # surrounding spaces are fine
    ("00036000291452", "0036000291452"),   # GTIN-14 with packaging digit 0
    ("36000291452", "0036000291452"),      # UPC-A that lost its leading zero
    (36000291452, "0036000291452"),        # ...or was stored as a number
    ("96385074", "0000096385074"),         # EAN-8 left-padded
    ("04252614", "0042100005264"),         # UPC-E expanded to UPC-A 042100005264
])
def test_to_gtin13_normalizes_valid_codes(code, expected):
    assert to_gtin13(code) == expected


@pytest.mark.parametrize("code", [
    None, "", "abc", "03600029145A", "036-000-291452",
    "036000291453",                # wrong check digit
    "4006381333932",               # wrong check digit
    "10036000291459",              # GTIN-14 for a case, not a consumer unit
    "128501447328300349",          # store-internal long code
    "1234565",                     # too short to be a retail barcode
    "00214036",                    # neither a valid EAN-8 nor UPC-E
])
def test_to_gtin13_rejects_invalid_codes(code):
    assert to_gtin13(code) is None


def test_upce_expansion_rules():
    assert upce_to_upca("04252614") == "042100005264"
    assert upce_to_upca("21234565") is None    # number system must be 0 or 1
    assert upce_to_upca("04252615") is None    # wrong check digit
    assert upce_to_upca("0425261") is None     # wrong length


@pytest.mark.parametrize("upc, expected", [
    ("0001111060903", "0011110609038"),   # Kroger Large Eggs (found in Open Food Facts)
    ("0003600029145", "0036000291452"),
    ("0400638133393", "4006381333931"),   # an EAN-13 product
])
def test_kroger_upc_gets_its_check_digit(upc, expected):
    assert kroger_upc_to_gtin13(upc) == expected


def test_kroger_conversion_matches_the_earlier_working_logic():
    upc = "0001111060903"
    core = upc.lstrip("0").zfill(11)
    assert kroger_upc_to_gtin13(upc) == "0" + core + gtin_check_digit(core)


@pytest.mark.parametrize("upc", [None, "", "0000000004011", "000000094011", "1001111060903",
                                 "00011110609A3", "00001111060903"])
def test_kroger_upc_rejects_plu_and_malformed_codes(upc):
    assert kroger_upc_to_gtin13(upc) is None
