"""Reading line provenance stored by quote extraction. Pure."""

from bido_back.comparison.loader import field_infos, review_issues


def test_review_issues_reads_the_recorded_line_issues() -> None:
    assert review_issues(
        {"_review": {"needs_review": True, "issues": ["currency_unverified"]}}
    ) == ("currency_unverified",)
    assert review_issues({"_review": {"needs_review": False, "issues": []}}) == ()


def test_review_issues_is_unknown_without_a_record() -> None:
    assert review_issues({}) is None
    assert review_issues({"_review": "broken"}) is None
    assert review_issues({"_review": {"needs_review": True}}) is None


def test_an_absent_value_is_not_a_low_confidence_value() -> None:
    infos = field_infos(
        {
            "unit_price": {"value": "0.039", "confidence": 0.95, "needs_review": False},
            # Not stated by the supplier: the extraction records confidence 0.
            "tooling_cost": {"value": None, "confidence": 0.0, "needs_review": False},
            "moq": {"confidence": 0.5},
        }
    )
    assert infos["unit_price"].confidence == 0.95
    assert infos["tooling_cost"].confidence is None
    assert infos["moq"].confidence == 0.5
