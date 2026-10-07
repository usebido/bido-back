"""Reading line provenance stored by quote extraction. Pure."""

from bido_back.comparison.loader import review_issues


def test_review_issues_reads_the_recorded_line_issues() -> None:
    assert review_issues(
        {"_review": {"needs_review": True, "issues": ["currency_unverified"]}}
    ) == ("currency_unverified",)
    assert review_issues({"_review": {"needs_review": False, "issues": []}}) == ()


def test_review_issues_is_unknown_without_a_record() -> None:
    assert review_issues({}) is None
    assert review_issues({"_review": "broken"}) is None
    assert review_issues({"_review": {"needs_review": True}}) is None
