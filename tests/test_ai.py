"""The AI provider contract: credentials, prices, the plugin declaration, message serialisation, and the
conformance kit run on the reference fake."""

import pytest

from marvin_integration_sdk.ai import (
    API_KEY,
    BASE_URL,
    AIConfigError,
    AIProvider,
    AIProviderPlugin,
    Credential,
    ImagePart,
    Message,
    ModelPrice,
    ToolCall,
    deserialize_messages,
    masked_credentials,
    price_for,
    read_credentials,
    serialize_messages,
)
from marvin_integration_sdk.ai.fake import FakeAIProvider, ScriptedTransport, plugin
from marvin_integration_sdk.ai.testing import AIProviderContract


class TestFakeProvider(AIProviderContract):
    @pytest.fixture
    def transport(self):
        return ScriptedTransport()

    @pytest.fixture
    def provider(self, transport):
        return FakeAIProvider(transport)


class _Bare(FakeAIProvider):
    """No optional capability: the kit checks each one is refused, not faked."""

    provider_type = "bare"
    display_name = "Bare"
    supports_vision = False
    supports_embeddings = False
    supports_tool_calls = False
    self_hosted = True
    prices = {}

    def complete_with_tools(self, *args, **kwargs):
        return AIProvider.complete_with_tools(self, *args, **kwargs)

    def embed(self, texts, model):
        return AIProvider.embed(self, texts, model)


class TestBareProvider(AIProviderContract):
    @pytest.fixture
    def transport(self):
        return ScriptedTransport()

    @pytest.fixture
    def provider(self, transport):
        return _Bare(transport)


def test_a_credential_reads_from_the_platform_setting_named_after_the_slug():
    assert API_KEY.env("openai") == "OPENAI_API_KEY"
    assert BASE_URL.env("azure") == "AZURE_BASE_URL"
    assert Credential("api_version").env("my-vendor") == "MY_VENDOR_API_VERSION"


def test_read_credentials_fills_defaults_and_names_what_is_missing():
    declared = (API_KEY, BASE_URL, Credential("api_version", default="2024-02-01"))
    assert read_credentials(declared, {"api_key": "k"}) == {"api_key": "k", "base_url": None, "api_version": "2024-02-01"}
    with pytest.raises(AIConfigError, match="missing credential.*api_key"):
        read_credentials(declared, {"api_key": ""})
    assert read_credentials(declared, {}, require=False)["api_key"] is None


def test_masked_credentials_hide_a_set_secret_only():
    declared = (API_KEY, BASE_URL)
    assert masked_credentials(declared, {"api_key": "placeholder", "base_url": "https://x"}) == {"api_key": "****", "base_url": "https://x"}
    assert masked_credentials(declared, {"api_key": None})["api_key"] is None


def test_price_for_takes_exact_ids_then_dated_snapshots_only():
    prices = {"gpt-5": ModelPrice(1.25, 10.0), "gpt-4o": ModelPrice(2.5, 10.0), "gpt-4o-mini": ModelPrice(0.15, 0.6)}
    assert price_for(prices, "gpt-4o") == prices["gpt-4o"]
    assert price_for(prices, "gpt-4o-2024-08-06") == prices["gpt-4o"]
    assert price_for(prices, "gpt-4o-mini-2024-07-18") == prices["gpt-4o-mini"]
    assert price_for(prices, "gpt-5.6-luna") is None
    assert ModelPrice(2.0, 8.0).cost(1_000_000, 500_000) == 6.0


def test_unpriced_and_self_hosted_estimates():
    assert FakeAIProvider.estimate_cost("fake-model", 1_000_000, 1_000_000) == 3.0
    assert FakeAIProvider.estimate_cost("other", 10, 10) is None
    assert _Bare.estimate_cost("anything", 10, 10) == 0.0


def test_capabilities_are_the_flags():
    assert FakeAIProvider.capabilities() == {"vision": True, "structured_output": True, "embeddings": True, "tool_calls": True, "model_pull": False}


@pytest.mark.parametrize("slug", ["", "OpenAI", "has space", "-lead", "x" * 41])
def test_plugin_refuses_a_bad_slug(slug):
    with pytest.raises(ValueError, match="invalid AI provider slug"):
        AIProviderPlugin(slug=slug, name="X", provider=FakeAIProvider)


def test_plugin_checks_its_provider():
    with pytest.raises(TypeError, match="must be an AIProvider subclass"):
        AIProviderPlugin(slug="fake", name="X", provider=dict)
    with pytest.raises(ValueError, match="provider_type is 'fake'"):
        AIProviderPlugin(slug="other", name="X", provider=FakeAIProvider)

    class _Twice(FakeAIProvider):
        credentials = (API_KEY, API_KEY)

    with pytest.raises(ValueError, match="declared twice"):
        AIProviderPlugin(slug="fake", name="X", provider=_Twice)
    assert plugin.slug == "fake"


def test_messages_survive_serialisation():
    messages = [
        Message("user", ["look", ImagePart(data="aGk=", mime_type="image/png")]),
        Message("assistant", "", tool_calls=[ToolCall(id="c1", name="t", arguments={"a": 1})]),
        Message("tool", "result", tool_call_id="c1"),
    ]
    assert deserialize_messages(serialize_messages(messages)) == messages


def test_the_fake_records_what_it_was_sent():
    transport = ScriptedTransport()
    provider = FakeAIProvider(transport)
    transport.reply_text("Hello.")
    assert provider.complete([Message("user", "hi")], "fake-model").content == "Hello."
    assert transport.requests[-1]["op"] == "complete"
    assert transport.requests[-1]["messages"] == [{"role": "user", "content": "hi"}]
