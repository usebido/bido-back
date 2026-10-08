"""Which product a buyer's words refer to (no database)."""

import pytest

from bido_back.purchasing.service import match_product

ONE = ["Motor EV-120"]
SEVERAL = ["Motor EV-120", "Battery Pack BP-48", "Inverter INV-9"]


@pytest.mark.parametrize(
    "wanted",
    ["esse produto", "Este produto", "o produto", "this product", "il prodotto", "", "isso"],
)
def test_a_pointer_means_the_only_product(wanted: str) -> None:
    assert match_product(ONE, wanted) == "Motor EV-120"


@pytest.mark.parametrize("wanted", ["esse produto", "this product", ""])
def test_a_pointer_is_ambiguous_with_several_products(wanted: str) -> None:
    assert match_product(SEVERAL, wanted) is None


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ("Motor EV-120", "Motor EV-120"),
        ("motor", "Motor EV-120"),
        ("motor ev120", "Motor EV-120"),
        ("battery", "Battery Pack BP-48"),
        ("BP-48", "Battery Pack BP-48"),
        ("inverter inv 9", "Inverter INV-9"),
    ],
)
def test_partial_and_close_names(wanted: str, expected: str) -> None:
    assert match_product(SEVERAL, wanted) == expected


def test_a_different_product_is_not_guessed() -> None:
    assert match_product(ONE, "inverter") is None
    assert match_product([], "motor") is None
