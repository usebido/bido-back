"""Decimals as people read them: plain digits, never exponent notation.

`Decimal.normalize()` drops trailing zeros, but writes a round number in
exponent form: 90.0000 becomes `9E+1`, 1000 becomes `1E+3`. `str()` of such a
value, and pydantic's JSON for it, is what a model input, an email, an approval
or the API would then show, and a writer model copies `9E+1` into the email.
Every amount or quantity Bido decides goes through these instead.

Values quantized to at most six decimals (every price Bido decides is) always
print as plain digits: Python writes `Decimal` in exponent form only for
positive exponents, which these remove, and below 0.000001.
"""

from decimal import ROUND_HALF_UP, Decimal


def plain_decimal(value: Decimal) -> Decimal:
    """`value` without trailing zeros and never in exponent form.

    90.0000 -> 90, 89.90 -> 89.9, 1E+3 -> 1000, 0.04000 -> 0.04: the same number,
    whose `str()` (and JSON) is plain digits.
    """
    if not value.is_finite():
        raise ValueError("not a finite decimal")
    if value.is_zero():
        return Decimal(0)
    return Decimal(format(value.normalize(), "f"))


def format_decimal(value: Decimal, places: int | None = None) -> str:
    """Plain digits: "90", "89.9", "0.0001", never "9E+1".

    With `places`, rounded half-up to exactly that many decimals: "90.00".
    """
    if places is None:
        return format(plain_decimal(value), "f")
    if places < 0:
        raise ValueError("places must not be negative")
    if not value.is_finite():
        raise ValueError("not a finite decimal")
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(rounded.copy_abs() if rounded.is_zero() else rounded, "f")
