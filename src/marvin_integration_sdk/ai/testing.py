"""Conformance kit for AI provider plugins (needs pytest). No network: the provider talks to a fake.

Implement ``FakeTransport`` for your vendor: it plays the vendor's server, turning the kit's
vendor-neutral replies ("answer this text", "call this tool") into your API's wire format, and records
what the provider sent. With an HTTP SDK that is usually a mock transport handed to the client (for
httpx: ``httpx.MockTransport``). Then subclass the contract and supply the two fixtures:

    from marvin_integration_sdk.ai.testing import AIProviderContract

    class TestMyProvider(AIProviderContract):
        @pytest.fixture
        def transport(self):
            return MyFakeTransport()

        @pytest.fixture
        def provider(self, transport):
            return MyProvider(api_key="test-key", http_client=transport.client())

Every ``test_*`` method then runs against it. Capabilities are honest both ways: a provider that says
``supports_tool_calls`` must round-trip tool calls; one that doesn't must refuse with
``NotImplementedError``. The class names don't start with ``Test``, so pytest only collects your
subclasses. The SDK runs the same kit against its reference fake (``marvin_integration_sdk.ai.fake``).
"""

from __future__ import annotations

import json
import pytest

from . import (
    AIProvider,
    AIProviderPlugin,
    CompletionResult,
    ImagePart,
    Message,
    ModelPrice,
    ToolCall,
    ToolDefinition,
    masked_credentials,
    read_credentials,
)
from .fake import FakeTransport

__all__ = ["AIProviderContract", "FakeTransport"]

IMAGE_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
LOOKUP = ToolDefinition(
    name="lookup_entry", description="Find an entry by title.", input_schema={"type": "object", "properties": {"q": {"type": "string"}}}
)


class AIProviderContract:
    """What every ``AIProvider`` must do."""

    @pytest.fixture
    def transport(self) -> FakeTransport:
        raise NotImplementedError("override the `transport` fixture to return your vendor's FakeTransport")

    @pytest.fixture
    def provider(self, transport) -> AIProvider:
        raise NotImplementedError("override the `provider` fixture to return a provider that talks to `transport`")

    @pytest.fixture
    def model(self, provider) -> str:
        return type(provider).default_model or "test-model"

    # ── identity, credentials, prices ────────────────────────────────────────

    def test_declares_a_plugin(self, provider):
        cls = type(provider)
        assert isinstance(cls.display_name, str) and cls.display_name
        plugin = AIProviderPlugin(slug=cls.provider_type, name=cls.display_name, provider=cls)
        assert plugin.slug == cls.provider_type

    def test_builds_from_its_credentials(self, provider):
        cls = type(provider)
        given = {c.key: c.default or ("https://example.invalid" if c.key.endswith("url") else "test-value") for c in cls.credentials}
        built = cls.from_credentials(read_credentials(cls.credentials, given))
        assert isinstance(built, cls)
        masked = masked_credentials(cls.credentials, given)
        for c in cls.credentials:
            assert (masked[c.key] == "****") is c.secret

    def test_prices_are_sane(self, provider):
        cls = type(provider)
        for model_id, price in cls.prices.items():
            assert isinstance(price, ModelPrice)
            assert price.input_per_1m >= 0 and price.output_per_1m >= 0
            assert cls.price(model_id) == price
            assert cls.price(f"{model_id}-2099-01-01") is not None
        if cls.self_hosted:
            assert cls.estimate_cost("anything", 1000, 1000) == 0.0
        else:
            for model_id, price in cls.prices.items():
                assert cls.estimate_cost(model_id, 1_000_000, 1_000_000) == round(price.input_per_1m + price.output_per_1m, 8)
            assert cls.estimate_cost("no-such-model-anywhere", 10, 10) is None

    # ── completions ──────────────────────────────────────────────────────────

    def test_complete_returns_the_answer_and_usage(self, provider, transport, model):
        transport.reply_text("Hello there.", prompt_tokens=11, completion_tokens=7)
        result = provider.complete([Message("system", "Be brief."), Message("user", "Say hello to Arthur.")], model)
        assert isinstance(result, CompletionResult)
        assert result.content == "Hello there."
        assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (11, 7, 18)
        assert isinstance(result.model, str) and result.model
        sent = transport.last_request()
        assert "Say hello to Arthur." in sent and "Be brief." in sent

    def test_a_vendor_error_is_raised(self, provider, transport, model):
        transport.reply_error(500, "the server is on fire")
        with pytest.raises(Exception):  # noqa: B017 — the vendor SDK's own error type
            provider.complete([Message("user", "hi")], model)

    def test_structured_output_is_a_dict(self, provider, transport, model):
        schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
        transport.reply_text(json.dumps({"tags": ["towel", "guide"]}))
        assert provider.complete_structured([Message("user", "Tag this.")], model, schema) == {"tags": ["towel", "guide"]}

    def test_an_operation_returns_the_answer_with_usage(self, provider, transport, model):
        schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
        transport.reply_text(json.dumps({"summary": "Don't panic."}), prompt_tokens=30, completion_tokens=5)
        parsed, result = provider.execute_operation([Message("user", "Summarise.")], model, schema)
        assert parsed == {"summary": "Don't panic."}
        assert (result.prompt_tokens, result.completion_tokens) == (30, 5)

    # ── tools ────────────────────────────────────────────────────────────────

    def test_tool_calls_come_back_decoded(self, provider, transport, model):
        if not type(provider).supports_tool_calls:
            with pytest.raises(NotImplementedError):
                provider.complete_with_tools([Message("user", "Find the towel entry.")], model, [LOOKUP])
            return
        transport.reply_tool_calls([ToolCall(id="call_1", name="lookup_entry", arguments={"q": "towel"})])
        result = provider.complete_with_tools([Message("user", "Find the towel entry.")], model, [LOOKUP])
        assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("call_1", "lookup_entry", {"q": "towel"})]
        assert "lookup_entry" in transport.last_request()

    def test_a_tool_result_goes_back_with_its_call_id(self, provider, transport, model):
        if not type(provider).supports_tool_calls:
            pytest.skip("provider does not support tool calls")
        transcript = [
            Message("user", "Find the towel entry."),
            Message("assistant", "", tool_calls=[ToolCall(id="call_1", name="lookup_entry", arguments={"q": "towel"})]),
            Message("tool", '{"title": "Towel", "id": 42}', tool_call_id="call_1"),
        ]
        transport.reply_text("It's entry 42.")
        result = provider.complete_with_tools(transcript, model, [LOOKUP])
        assert result.content == "It's entry 42."
        assert result.tool_calls == []
        sent = transport.last_request()
        assert "call_1" in sent and "Towel" in sent

    # ── vision, embeddings, models ───────────────────────────────────────────

    def test_an_image_reaches_the_vendor(self, provider, transport, model):
        if not type(provider).supports_vision:
            pytest.skip("provider does not support vision")
        transport.reply_text("A single pixel.")
        result = provider.complete([Message("user", ["What is this?", ImagePart(data=IMAGE_DATA, mime_type="image/png")])], model)
        assert result.content == "A single pixel."
        assert IMAGE_DATA in transport.last_request()

    def test_embeddings_are_one_vector_per_text(self, provider, transport):
        cls = type(provider)
        if not cls.supports_embeddings:
            with pytest.raises(NotImplementedError):
                provider.embed(["a"], "any")
            return
        transport.reply_embeddings([[0.25, 0.5], [0.75, 1.0]])
        assert provider.embed(["first", "second"], cls.default_embedding_model or "embed-model") == [[0.25, 0.5], [0.75, 1.0]]

    def test_list_models(self, provider, transport):
        transport.reply_models(["chat-a", "chat-b"])
        assert sorted(provider.list_models()) == ["chat-a", "chat-b"]

    def test_connection_ok(self, provider, transport):
        transport.reply_connection_ok()
        ok, message = provider.test_connection()
        assert ok is True and isinstance(message, str)

    def test_connection_failure_is_reported_not_raised(self, provider, transport):
        transport.reply_error(401, "Incorrect API key provided")
        ok, message = provider.test_connection()
        assert ok is False and isinstance(message, str) and message

    def test_model_pull_is_honest(self, provider):
        if not type(provider).supports_model_pull:
            with pytest.raises(NotImplementedError):
                provider.pull_model("anything")
