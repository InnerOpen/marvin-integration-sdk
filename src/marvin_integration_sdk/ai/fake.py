"""A reference fake: an in-memory AI provider and the scripted transport it talks to.

``FakeAIProvider`` passes the conformance kit, so it is the executable reading of the contract, and it
is the provider Marvin's tests use: no vendor, no network, every answer scripted.

    transport = ScriptedTransport()
    provider = FakeAIProvider(transport)
    transport.reply_text("Hello.")
    provider.complete([Message("user", "hi")], "fake-model").content  # "Hello."
    transport.requests[-1]  # what the provider sent: {"op": "complete", "model": ..., "messages": [...]}
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from . import (
    API_KEY,
    AIProvider,
    AIProviderPlugin,
    CompletionOptions,
    CompletionResult,
    Message,
    ModelPrice,
    ToolCall,
    ToolDefinition,
    serialize_messages,
)

__all__ = ["FakeAIProvider", "FakeAPIError", "FakeTransport", "ScriptedTransport", "plugin"]


class FakeTransport(ABC):
    """The vendor's server, scripted. Each ``reply_*`` queues the answer to the provider's next request."""

    @abstractmethod
    def reply_text(self, text: str, *, prompt_tokens: int = 11, completion_tokens: int = 7) -> None:
        """The model answers with ``text`` (also used for JSON answers to structured requests)."""

    @abstractmethod
    def reply_tool_calls(self, calls: list[ToolCall], *, prompt_tokens: int = 11, completion_tokens: int = 7) -> None:
        """The model asks to call these tools instead of answering."""

    @abstractmethod
    def reply_models(self, ids: list[str]) -> None:
        """The vendor lists these model ids."""

    @abstractmethod
    def reply_error(self, status: int, message: str) -> None:
        """The vendor refuses the request with this HTTP status."""

    def reply_embeddings(self, vectors: list[list[float]]) -> None:
        """One vector per input text. Only needed when the provider supports embeddings."""
        raise NotImplementedError

    def reply_connection_ok(self) -> None:
        """Whatever the provider's ``test_connection`` asks the vendor, answered successfully."""
        self.reply_models(["chat-a"])

    @abstractmethod
    def last_request(self) -> str:
        """The last request the provider sent, as text (its JSON body), so the kit can check that what
        it was given (a message, a tool result, an image) reached the vendor."""


class FakeAPIError(Exception):
    """What the fake vendor raises for ``reply_error``."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status_code = status


class ScriptedTransport(FakeTransport):
    """Queued replies, recorded requests. A request with nothing queued gets an empty answer."""

    def __init__(self) -> None:
        self.replies: deque[dict] = deque()
        self.requests: list[dict] = []

    def reply_text(self, text: str, *, prompt_tokens: int = 11, completion_tokens: int = 7) -> None:
        self.replies.append({"text": text, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})

    def reply_tool_calls(self, calls: list[ToolCall], *, prompt_tokens: int = 11, completion_tokens: int = 7) -> None:
        self.replies.append({"tool_calls": list(calls), "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})

    def reply_models(self, ids: list[str]) -> None:
        self.replies.append({"models": list(ids)})

    def reply_embeddings(self, vectors: list[list[float]]) -> None:
        self.replies.append({"embeddings": [list(v) for v in vectors]})

    def reply_error(self, status: int, message: str) -> None:
        self.replies.append({"error": (status, message)})

    def last_request(self) -> str:
        return json.dumps(self.requests[-1], default=str) if self.requests else ""

    def send(self, request: dict) -> dict:
        self.requests.append(request)
        reply = self.replies.popleft() if self.replies else {}
        if "error" in reply:
            raise FakeAPIError(*reply["error"])
        return reply


class FakeAIProvider(AIProvider):
    """Every capability, answered by a ``ScriptedTransport``."""

    provider_type = "fake"
    display_name = "Fake (tests)"
    credentials = (API_KEY,)
    supports_vision = True
    supports_structured_output = True
    supports_embeddings = True
    supports_tool_calls = True
    prices = {"fake-model": ModelPrice(input_per_1m=1.0, output_per_1m=2.0)}
    default_model = "fake-model"
    suggested_models = ("fake-model",)
    default_embedding_model = "fake-embed"

    def __init__(self, transport: ScriptedTransport | None = None, api_key: str | None = None) -> None:
        self.transport = transport or ScriptedTransport()
        self.api_key = api_key

    @classmethod
    def from_credentials(cls, values: Mapping[str, Any]) -> FakeAIProvider:
        return cls(api_key=values.get("api_key"))

    def _request(self, op: str, model: str, messages: list[Message], options: CompletionOptions | None, **extra) -> dict:
        return self.transport.send(
            {"op": op, "model": model, "messages": serialize_messages(messages), "options": asdict(options or CompletionOptions()), **extra}
        )

    @staticmethod
    def _result(reply: dict, model: str) -> CompletionResult:
        pt, ct = int(reply.get("prompt_tokens", 0)), int(reply.get("completion_tokens", 0))
        calls = list(reply.get("tool_calls") or [])
        return CompletionResult(
            content=str(reply.get("text", "")),
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=pt + ct,
            model=model,
            raw=dict(reply),
            tool_calls=calls,
            stop_reason="tool_calls" if calls else "stop",
        )

    def complete(self, messages: list[Message], model: str, options: CompletionOptions | None = None) -> CompletionResult:
        return self._result(self._request("complete", model, messages, options), model)

    def complete_with_tools(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition],
        options: CompletionOptions | None = None,
        tool_choice: str = "auto",
    ) -> CompletionResult:
        reply = self._request("complete_with_tools", model, messages, options, tools=[asdict(t) for t in tools], tool_choice=tool_choice)
        return self._result(reply, model)

    def complete_structured(self, messages: list[Message], model: str, output_schema: dict, options: CompletionOptions | None = None) -> dict:
        return self.execute_operation(messages, model, output_schema, options)[0]

    def execute_operation(self, messages, model, output_schema, options=None):
        result = self._result(self._request("structured", model, messages, options, schema=output_schema), model)
        return json.loads(result.content or "{}"), result

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return list(self.transport.send({"op": "embed", "model": model, "input": list(texts)}).get("embeddings") or [])

    def list_models(self) -> list[str]:
        return list(self.transport.send({"op": "models"}).get("models") or [])

    def test_connection(self) -> tuple[bool, str]:
        try:
            return True, f"Connected — {len(self.list_models())} models available"
        except Exception as e:  # noqa: BLE001 — reported, never raised
            return False, str(e)


plugin = AIProviderPlugin(slug="fake", name="Fake (tests)", provider=FakeAIProvider, description="Scripted answers for tests.")
