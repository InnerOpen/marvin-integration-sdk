"""post_limits (0.19.0): a posting provider states its network's numbers; Marvin checks captions against them."""

import pytest

from marvin_integration_sdk import IntegrationProvider, check_post_limits, post_limits_of

INSTAGRAM = {"max": 2200, "unit": "characters", "visible": 125, "hashtags_max": 30, "links": "not_clickable"}


def _provider(limits):
    return type("P", (IntegrationProvider,), {"slug": "p", "name": "P", "post_limits": limits})


def test_a_provider_declares_none_by_default():
    assert IntegrationProvider.post_limits == {} and post_limits_of(_provider({})) is None


def test_a_full_declaration_reads_back_as_written():
    assert post_limits_of(_provider(INSTAGRAM)) == INSTAGRAM


def test_defaults_fill_in_what_is_left_out():
    assert post_limits_of(_provider({"max": 300, "unit": "graphemes"})) == {
        "max": 300, "unit": "graphemes", "visible": None, "hashtags_max": None, "links": "clickable",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("limits", "problem"),
    [
        ({"unit": "characters"}, "max must be a positive integer"),
        ({"max": 0}, "max must be a positive integer"),
        ({"max": True}, "max must be a positive integer"),
        ({"max": 500, "unit": "words"}, "unit must be one of"),
        ({"max": 500, "visible": -1}, "visible must be a positive integer"),
        ({"max": 500, "hashtags_max": "30"}, "hashtags_max must be a positive integer"),
        ({"max": 500, "links": "maybe"}, "links must be one of"),
        ("2200", "post_limits must be a dict"),
    ],
)
def test_an_unusable_declaration_is_named_and_ignored(limits, problem):
    assert any(problem in p for p in check_post_limits(limits))
    if isinstance(limits, dict):
        assert post_limits_of(_provider(limits)) is None
