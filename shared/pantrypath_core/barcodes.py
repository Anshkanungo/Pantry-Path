"""Barcode helpers: write every store's and Open Food Facts' codes as one GTIN-13.

The same product shows up as a 12-digit UPC-A on the package, a 13-digit EAN-13 in
Open Food Facts, a GTIN-14 in some feeds, and 13 digits *without* a check digit in
Kroger's API. Matching products across sources only works once they all use the
same form, so everything is normalized to GTIN-13 (13 digits, valid check digit).
"""

PLU_MAX_DIGITS = 5  # produce price-look-up codes (4011 = bananas) are not barcodes


def gtin_check_digit(digits: str) -> str:
    """Check digit for a GTIN body (every digit except the check digit).

    Weights alternate 3, 1, 3, ... from the rightmost digit, so leading zeros don't
    change the result: that is why left-padding a valid code to 13 digits is safe.
    """
    if not digits or not digits.isdigit():
        raise ValueError(f"GTIN body must be digits, got {digits!r}")
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def _valid(code: str) -> bool:
    return gtin_check_digit(code[:-1]) == code[-1]


def upce_to_upca(code: str) -> str | None:
    """Expand an 8-digit UPC-E (zero-suppressed UPC-A on small US packs) to 12-digit UPC-A.

    Returns None unless the number system digit is 0 or 1 and the check digit matches.
    """
    if len(code) != 8 or not code.isdigit() or code[0] not in "01":
        return None
    system, d, check = code[0], code[1:7], code[7]
    last = d[5]
    if last in "012":
        body = d[0:2] + last + "0000" + d[2:5]
    elif last == "3":
        body = d[0:3] + "00000" + d[3:5]
    elif last == "4":
        body = d[0:4] + "00000" + d[4]
    else:
        body = d[0:5] + "0000" + last
    upca = system + body + check
    return upca if _valid(upca) else None


def to_gtin13(code: str | int | None) -> str | None:
    """Normalize a barcode to GTIN-13, or None when it isn't a valid retail barcode.

    - 13 digits (EAN-13): kept when the check digit is valid.
    - 14 digits (GTIN-14): the leading packaging digit must be 0 and is dropped.
    - 8 to 12 digits (EAN-8, UPC-A, codes whose leading zeros were lost): left-padded
      when the check digit is valid. An 8-digit code that isn't a valid EAN-8 is
      tried as a UPC-E and expanded to its UPC-A.
    - Anything else (letters, wrong check digit, store-internal codes): None.
    """
    if code is None:
        return None
    s = str(code).strip()
    if not s.isdigit():
        return None
    if len(s) == 14:
        if s[0] != "0":
            return None
        s = s[1:]
    if len(s) == 13:
        return s if _valid(s) else None
    if 8 <= len(s) <= 12:
        if _valid(s):
            return s.zfill(13)
        if len(s) == 8:
            upca = upce_to_upca(s)
            return "0" + upca if upca else None
    return None


def kroger_upc_to_gtin13(upc: str | None) -> str | None:
    """Kroger's 13-digit 'upc' (no check digit) -> GTIN-13, e.g. '0001111060903' -> '0011110609038'.

    Kroger left-pads the code without its check digit to 13 digits, so the last 12
    digits are the GTIN-13 body. Produce PLU codes (e.g. '0000000004011') give None.
    """
    if upc is None:
        return None
    s = str(upc).strip()
    if len(s) != 13 or not s.isdigit() or s[0] != "0":
        return None
    if len(s.lstrip("0")) <= PLU_MAX_DIGITS:
        return None
    body = s[1:]
    return body + gtin_check_digit(body)
