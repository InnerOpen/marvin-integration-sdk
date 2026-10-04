"""Error policies: a provider names its failures and declares how each is handled; the core applies it."""

import logging
import pickle

import pytest

from marvin_integration_sdk import (
    INTEGRATION_REGISTRY,
    Handle,
    IntegrationContext,
    IntegrationError,
    IntegrationProvider,
    ProviderAction,
    Retry,
    policy_info,
    register_provider,
    resolve_policy,
)

REVIEW = Handle(review=True)
NOTIFY = Handle(notify=True)
SUCCEED = Handle(succeed=True)
BACKOFF = Handle(retry=Retry(backoff=(120, 600, 3600)), then=REVIEW)


class _Shop(IntegrationProvider):
    slug = "shop"
    name = "Shop"
    error_policy = {"rate_limited": BACKOFF, "*": NOTIFY}
    actions = (
        ProviderAction(key="post", label="Post", error_policy={"duplicate": SUCCEED, "*": REVIEW}),
        ProviderAction(key="ping", label="Ping"),
    )


# --- IntegrationError ---


def test_an_error_carries_its_code_partial_and_retry_hint():
    err = IntegrationError("Too many requests", code="rate_limited", partial={"posted": 3}, retry_after=30.0)
    assert str(err) == "Too many requests"
    assert (err.code, err.partial, err.retry_after) == ("rate_limited", {"posted": 3}, 30.0)
    assert isinstance(err, ValueError)


def test_an_error_defaults_to_an_unknown_code_with_nothing_saved():
    err = IntegrationError("boom")
    assert (err.code, err.partial, err.retry_after) == ("unknown", None, None)


def test_an_error_survives_pickling():
    # The core may move a failure across a process boundary before it applies the policy.
    err = pickle.loads(pickle.dumps(IntegrationError("Too many requests", code="rate_limited", partial={"posted": 3})))
    assert str(err) == "Too many requests"
    assert (err.code, err.partial) == ("rate_limited", {"posted": 3})


# --- Retry ---


def test_delay_for_follows_the_backoff_then_repeats_the_last_delay():
    retry = Retry(backoff=(120, 600, 3600), max_attempts=5)
    assert [retry.delay_for(n) for n in range(1, 6)] == [120, 600, 3600, 3600, 3600]


def test_max_attempts_defaults_to_the_length_of_the_backoff():
    assert Retry(backoff=(1, 2, 3)).attempts == 3
    assert Retry(backoff=(1,), max_attempts=4).attempts == 4


def test_attempts_are_one_based():
    with pytest.raises(ValueError):
        Retry(backoff=(1,)).delay_for(0)


def test_waiting_for_recovery_needs_no_backoff():
    retry = Retry(backoff=(), on_recovery=True)
    assert (retry.attempts, retry.delay_for(1)) == (1, 0.0)


# --- resolve_policy ---


@pytest.mark.parametrize(
    "action, code, expected",
    [
        ("post", "duplicate", SUCCEED),  # action[code]
        ("post", "rate_limited", BACKOFF),  # provider[code] beats action["*"]
        ("post", "timeout", REVIEW),  # action["*"]
        ("ping", "timeout", NOTIFY),  # provider["*"]
        ("missing", "rate_limited", BACKOFF),  # an undeclared action still gets the provider's policy
    ],
)
def test_the_most_specific_declaration_wins(action, code, expected):
    assert resolve_policy(_Shop(), action, code) is expected


def test_no_declaration_resolves_to_none():
    class _Plain(IntegrationProvider):
        slug = "plain"
        name = "Plain"
        actions = (ProviderAction(key="ping", label="Ping"),)

    assert resolve_policy(_Plain(), "ping", "timeout") is None
    assert _Plain.error_policy == {} and _Plain.actions[0].error_policy == {}


def test_resolve_accepts_the_class_as_well_as_the_instance():
    assert resolve_policy(_Shop, "post", "duplicate") is SUCCEED


# --- to_dict / describe ---


def test_describe_reads_as_a_sentence():
    assert BACKOFF.describe() == "retry 3× (2m, 10m, 1h), then send to review"
    assert Handle(retry=Retry(backoff=(60, 5400), max_attempts=4)).describe() == "retry 4× (1m, 1h30m, then every 1h30m), then fail"
    assert Handle(notify=True, retry=Retry(backoff=(), on_recovery=True)).describe() == (
        "notify admins, wait for the connection to recover, then retry once, then fail"
    )
    assert Handle(succeed=True).describe() == "treat as success"
    assert Handle().describe() == "fail"


def test_to_dict_is_nested_and_json_safe():
    assert BACKOFF.to_dict() == {
        "review": False,
        "notify": False,
        "succeed": False,
        "retry": {"backoff": [120, 600, 3600], "max_attempts": 3, "on_recovery": False},
        "then": {"review": True, "notify": False, "succeed": False, "retry": None, "then": None, "summary": "send to review"},
        "summary": "retry 3× (2m, 10m, 1h), then send to review",
    }


def test_policies_reach_info():
    info = _Shop().info()
    assert info["error_policy"] == policy_info(_Shop())
    assert info["error_policy"]["provider"]["rate_limited"] == BACKOFF.to_dict()
    assert info["error_policy"]["actions"] == {"post": {"duplicate": SUCCEED.to_dict(), "*": REVIEW.to_dict()}, "ping": {}}
    assert info["actions"][0]["error_policy"]["duplicate"]["succeed"] is True


# --- IntegrationContext ---


def _ctx(**kwargs):
    return IntegrationContext(config={}, secret=None, logger=logging.getLogger("test"), http=None, **kwargs)


def test_a_context_built_the_old_way_still_works():
    ctx = _ctx()
    assert (ctx.resume, ctx.idempotency_seed) == (None, None)


def test_idempotency_keys_are_stable_across_a_retry_chain():
    first, retry = _ctx(idempotency_seed="entry-7:post"), _ctx(idempotency_seed="entry-7:post", resume={"posted": 3})
    assert first.idempotency_key("charge", 42) == retry.idempotency_key("charge", 42)
    assert first.idempotency_key("charge", 42) != first.idempotency_key("charge", 43)
    assert first.idempotency_key("charge", 42) != _ctx(idempotency_seed="entry-8:post").idempotency_key("charge", 42)


def test_without_a_seed_every_key_is_fresh():
    ctx = _ctx()
    assert ctx.idempotency_key("charge") != ctx.idempotency_key("charge")


# --- validation ---


def _register(**attrs):
    cls = type("_Bad", (IntegrationProvider,), {"slug": "bad", "name": "Bad", **attrs})
    try:
        return register_provider(cls)
    finally:
        INTEGRATION_REGISTRY.pop("bad", None)


def test_a_valid_policy_registers():
    _register(error_policy={"rate_limited": BACKOFF, "*": REVIEW})


@pytest.mark.parametrize(
    "attrs, message",
    [
        ({"error_policy": {"rate_limited": {"retry": (60,)}}}, r"provider 'bad': error_policy\['rate_limited'\] must be a Handle, got dict"),
        ({"error_policy": [REVIEW]}, "error_policy must be a dict"),
        ({"error_policy": {"": REVIEW}}, "keys must be non-empty error codes"),
        ({"actions": (ProviderAction(key="post", label="Post", error_policy={"*": True}),)}, "provider 'bad' action 'post'"),
    ],
)
def test_a_malformed_policy_is_rejected_at_registration(attrs, message):
    with pytest.raises(TypeError, match=message):
        _register(**attrs)
    assert "bad" not in INTEGRATION_REGISTRY


@pytest.mark.parametrize(
    "build, error",
    [
        (lambda: Handle(retry=(60, 300)), TypeError),
        (lambda: Handle(review="yes"), TypeError),
        (lambda: Handle(then=REVIEW), ValueError),  # nothing to exhaust
        (lambda: Handle(succeed=True, retry=Retry(backoff=(1,))), ValueError),  # a retry would re-run later steps
        (lambda: Handle(retyr=Retry(backoff=(1,))), TypeError),  # unknown key
        (lambda: Retry(backoff=()), ValueError),
        (lambda: Retry(backoff=(-1,)), TypeError),
        (lambda: Retry(backoff=(1,), max_attempts=0), ValueError),
    ],
)
def test_a_malformed_handle_is_rejected(build, error):
    with pytest.raises(error):
        build()


def test_any_code_name_is_the_providers_business():
    _register(error_policy={"Some Weird.Code!": REVIEW})
