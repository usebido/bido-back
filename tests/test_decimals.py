"""Decimals as people read them: never exponent notation (email audit F1)."""

from decimal import Decimal

import pytest
from pydantic import BaseModel

from bido_back.decimals import format_decimal, plain_decimal


@pytest.mark.parametrize(
    ("value", "plain"),
    [
        ("90.0000", "90"),
        ("100", "100"),
        ("1000.00", "1000"),
        ("1E+3", "1000"),
        ("50", "50"),
        ("18.00", "18"),
        ("89.90", "89.9"),
        ("0.0400", "0.04"),
        ("0.000001", "0.000001"),
        ("0E-6", "0"),
        ("-0.00", "0"),
        ("123456789012.123456", "123456789012.123456"),
    ],
)
def test_plain_decimals_keep_the_value_and_print_without_an_exponent(
    value: str, plain: str
) -> None:
    result = plain_decimal(Decimal(value))
    assert result == Decimal(value)
    assert str(result) == plain
    assert format_decimal(Decimal(value)) == plain


def test_pydantic_writes_a_plain_decimal_as_plain_digits() -> None:
    class Ask(BaseModel):
        target: Decimal

    assert Ask(target=Decimal("90.0000").normalize()).model_dump_json() == '{"target":"9E+1"}'
    assert Ask(target=plain_decimal(Decimal("90.0000"))).model_dump_json() == '{"target":"90"}'


def test_fixed_places_round_half_up() -> None:
    assert format_decimal(Decimal("90"), 2) == "90.00"
    assert format_decimal(Decimal("1E+3"), 2) == "1000.00"
    assert format_decimal(Decimal("45.6677"), 2) == "45.67"
    assert format_decimal(Decimal("0.125"), 2) == "0.13"
    assert format_decimal(Decimal("-0.001"), 2) == "0.00"
    with pytest.raises(ValueError):
        format_decimal(Decimal("1"), -1)
    with pytest.raises(ValueError):
        plain_decimal(Decimal("NaN"))
