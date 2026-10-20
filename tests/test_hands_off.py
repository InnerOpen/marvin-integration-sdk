"""Hand-offs: an action declares that it starts work elsewhere, and the provider maps the service's words to Marvin's."""

import json

import pytest

from marvin_integration_sdk import (
    INTEGRATION_REGISTRY,
    RESULT_FAILED,
    RESULT_IN_FLIGHT,
    RESULT_STATUSES,
    RESULT_SUCCEEDED,
    HandsOff,
    IntegrationProvider,
    ProviderAction,
    hands_off_problems,
    register_provider,
)
from marvin_integration_sdk.testing import IntegrationContract

N8N_WORDS = {"success": RESULT_SUCCEEDED, "error": RESULT_FAILED, "waiting": RESULT_IN_FLIGHT}

TRIGGER = ProviderAction(
    key="trigger",
    label="Trigger",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"execution_id": {"type": "string"}, "execution_url": {"type": "string"}}},
    hands_off=HandsOff(remote_id="execution_id", remote_url="execution_url", target="path", statuses=N8N_WORDS),
)


class _Flow(IntegrationProvider):
    slug = "flow"
    name = "Flow"
    actions = (TRIGGER, ProviderAction(key="list", label="List"))


class TestFlowContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return _Flow()


def test_marvins_three_statuses_are_in_flight_succeeded_and_failed():
    assert RESULT_STATUSES == ("in_flight", "succeeded", "failed")


def test_an_action_declares_no_hand_off_unless_it_says_so():
    assert ProviderAction(key="ping", label="Ping").hands_off is None


def test_the_catalog_entry_projects_the_hand_off_as_plain_json():
    info = _Flow().info()
    json.dumps(info)
    by_key = {a["key"]: a for a in info["actions"]}
    assert by_key["trigger"]["hands_off"] == {
        "remote_id": "execution_id",
        "remote_url": "execution_url",
        "target": "path",
        "statuses": N8N_WORDS,
    }
    assert by_key["list"]["hands_off"] is None


def test_a_provider_maps_its_own_words_to_marvins():
    flow = _Flow()
    assert [flow.result_status(w) for w in ("success", "error", "waiting")] == ["succeeded", "failed", "in_flight"]


def test_the_mapping_ignores_case_and_surrounding_space():
    assert _Flow().result_status("  Success ") == "succeeded"


def test_marvins_own_words_need_no_entry():
    flow = _Flow()
    assert [flow.result_status(w) for w in RESULT_STATUSES] == list(RESULT_STATUSES)


def test_a_word_the_provider_never_declared_maps_to_nothing():
    flow = _Flow()
    assert flow.result_status("exploded") is None
    assert flow.result_status("") is None
    assert flow.result_status(None) is None


def test_the_mapping_can_be_limited_to_one_action():
    class _Two(IntegrationProvider):
        slug = "two"
        name = "Two"
        actions = (
            ProviderAction(key="a", label="A", hands_off=HandsOff(statuses={"done": RESULT_SUCCEEDED})),
            ProviderAction(key="b", label="B", hands_off=HandsOff(statuses={"done": RESULT_FAILED})),
        )

    two = _Two()
    assert two.result_status("done", action="a") == "succeeded"
    assert two.result_status("done", action="b") == "failed"
    assert two.result_status("done") == "succeeded"  # no action named: the first declaration that knows the word


def test_a_provider_can_override_the_mapping_for_words_that_need_more_than_a_lookup():
    class _Numeric(_Flow):
        def result_status(self, word, action=None):
            return RESULT_FAILED if str(word).startswith("5") else super().result_status(word, action)

    assert _Numeric().result_status("503") == "failed"
    assert _Numeric().result_status("success") == "succeeded"


def test_a_status_that_is_not_one_of_marvins_three_is_a_problem():
    action = ProviderAction(key="go", label="Go", hands_off=HandsOff(statuses={"ok": "done"}))
    assert any("'done'" in p for p in hands_off_problems(action))


def test_an_empty_status_word_is_a_problem():
    action = ProviderAction(key="go", label="Go", hands_off=HandsOff(statuses={" ": RESULT_FAILED}))
    assert hands_off_problems(action)


def test_keys_that_the_declared_schemas_do_not_have_are_problems():
    action = ProviderAction(
        key="go",
        label="Go",
        input_schema={"properties": {"path": {}}},
        output_schema={"properties": {"id": {}}},
        hands_off=HandsOff(remote_id="run_id", remote_url="url", target="where"),
    )
    problems = hands_off_problems(action)
    assert len(problems) == 3
    assert all("is not a property" in p for p in problems)


def test_keys_are_not_checked_when_the_action_declares_no_schema():
    action = ProviderAction(key="go", label="Go", hands_off=HandsOff(remote_id="run_id", target="where"))
    assert hands_off_problems(action) == []


def test_registering_a_provider_with_a_malformed_hand_off_is_refused():
    class _Bad(IntegrationProvider):
        slug = "bad_hands_off"
        name = "Bad"
        actions = (ProviderAction(key="go", label="Go", hands_off=HandsOff(statuses={"ok": "fine"})),)

    with pytest.raises(ValueError, match="bad_hands_off"):
        register_provider(_Bad)
    assert "bad_hands_off" not in INTEGRATION_REGISTRY


def test_registering_a_provider_with_a_well_formed_hand_off_works():
    class _Good(_Flow):
        slug = "good_hands_off"

    register_provider(_Good)
    try:
        assert INTEGRATION_REGISTRY["good_hands_off"].get_action("trigger").hands_off.remote_id == "execution_id"
    finally:
        INTEGRATION_REGISTRY.pop("good_hands_off", None)
