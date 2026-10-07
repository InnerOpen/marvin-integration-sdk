"""The AI provider contract: a model vendor (OpenAI, Anthropic, Ollama…) as a site-wide plugin.

An AI provider plugin is a package the platform operator installs, never a workspace. It exposes one
``AIProviderPlugin`` per vendor API through a ``marvin.ai_providers`` entry point. Every workspace can
then pick that provider in its AI settings, with the platform's credentials or its own.

A provider is a subclass of ``AIProvider``. It declares:

- **credentials** (``Credential``): what it needs to connect (``api_key``, ``base_url``, or an option
  such as Azure's ``api_version``). Marvin fills them from the platform's settings (``<SLUG>_<KEY>``,
  e.g. ``OPENAI_API_KEY``) or from a workspace's secret and provider row, masks secrets wherever it
  shows them, and builds the provider with ``from_credentials``.
- **capabilities**: ``supports_vision``, ``supports_structured_output``, ``supports_embeddings``,
  ``supports_tool_calls``, ``supports_model_pull``. Marvin only offers what the flags say.
- **prices** (``ModelPrice`` per model id) and ``self_hosted``: how Marvin estimates what a run cost.
  A dated snapshot (``gpt-4o-2024-08-06``) takes the price of the id it extends.
- **models**: ``default_model``, ``suggested_models`` for the AI settings' model picker, and
  ``default_embedding_model``.

Messages, tool calls and results use the vendor-neutral types below; the provider translates them to
its own API. Run the conformance kit (``marvin_integration_sdk.ai.testing``) against your provider over
a fake transport: Marvin runs the same kit against its reference fake.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

__all__ = [
    "API_KEY",
    "BASE_URL",
    "CAPABILITIES",
    "ENTRY_POINT_GROUP",
    "AIConfigError",
    "AIProvider",
    "AIProviderPlugin",
    "CompletionOptions",
    "CompletionResult",
    "Credential",
    "ImagePart",
    "Message",
    "ModelPrice",
    "ToolCall",
    "ToolDefinition",
    "deserialize_messages",
    "masked_credentials",
    "price_for",
    "read_credentials",
    "serialize_messages",
]

ENTRY_POINT_GROUP = "marvin.ai_providers"
"""The entry-point group Marvin discovers AI provider plugins from."""

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
MASK = "****"


class AIConfigError(ValueError):
    """A provider isn't installed or can't be built from its credentials. The message never carries a secret."""


# --------------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """One value a provider needs to connect.

    ``api_key`` comes from a secret (the platform's ``<SLUG>_API_KEY``, or the workspace secret its AI
    settings name); ``base_url`` from ``<SLUG>_BASE_URL`` or the workspace's provider row; any other key
    is an option, read from ``<SLUG>_<KEY>`` or the provider row's metadata.
    """

    key: str
    label: str = ""
    secret: bool = False
    """Masked wherever Marvin shows the value (admin pages, logs)."""
    required: bool = False
    default: str | None = None
    help: str = ""

    def env(self, slug: str) -> str:
        """The platform setting Marvin reads this from, e.g. ``OPENAI_API_KEY``."""
        return f"{slug}_{self.key}".upper().replace("-", "_")


API_KEY = Credential("api_key", "API key", secret=True, required=True)
"""The usual credential: a vendor API key, kept in a secret."""
BASE_URL = Credential("base_url", "Base URL", help="Leave empty for the vendor's own endpoint.")
"""An optional endpoint override (a proxy, a compatible server)."""


def read_credentials(credentials: Iterable[Credential], source: Mapping[str, Any], *, require: bool = True) -> dict[str, Any]:
    """The declared credentials' values from ``source`` (keyed by ``Credential.key``), defaults filled
    in. With ``require``, raises ``AIConfigError`` naming every required credential that has no value."""
    values: dict[str, Any] = {}
    missing: list[str] = []
    for c in credentials:
        value = source.get(c.key)
        if value is None or value == "":
            value = c.default
        if c.required and (value is None or value == ""):
            missing.append(c.key)
        values[c.key] = value
    if require and missing:
        raise AIConfigError(f"missing credential(s): {', '.join(missing)}")
    return values


def masked_credentials(credentials: Iterable[Credential], values: Mapping[str, Any]) -> dict[str, Any]:
    """``values`` safe to show: a secret that is set reads ``****``."""
    secret = {c.key for c in credentials if c.secret}
    return {k: (MASK if k in secret and v not in (None, "") else v) for k, v in values.items()}


# --------------------------------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, input (prompt) and output (completion) separately."""

    input_per_1m: float
    output_per_1m: float

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round((prompt_tokens / 1_000_000) * self.input_per_1m + (completion_tokens / 1_000_000) * self.output_per_1m, 8)


def price_for(prices: Mapping[str, ModelPrice], model_id: str) -> ModelPrice | None:
    """The exact model id first, then the longest known id it extends with a "-" (dated snapshots like
    ``gpt-4o-2024-08-06``). ``gpt-5.6-luna`` does not fall back to ``gpt-5``. None when unknown."""
    if model_id in prices:
        return prices[model_id]
    extended = [known for known in prices if model_id.startswith(known + "-")]
    return prices[max(extended, key=len)] if extended else None


# --------------------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------------------


@dataclass
class ImagePart:
    """Provider-agnostic inline image for multimodal messages.

    ``data`` is base64-encoded image bytes; ``mime_type`` is e.g. ``image/png``. A multimodal Message
    carries content = list mixing str (text) and ImagePart; each provider translates ImagePart into its
    own format.
    """

    data: str
    mime_type: str


@dataclass
class ToolDefinition:
    """A tool the model may call. ``input_schema`` is a JSON schema of the tool's arguments; each
    provider translates it into its own tool/function format in ``complete_with_tools``."""

    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    """A model's request to call a tool. ``id`` correlates the call with its result (echoed back on the
    role="tool" Message); ``arguments`` is the decoded argument object."""

    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list  # str, or list mixing str (text) and ImagePart (image) for multimodal
    # Tool-calling round-trip (both optional; only used on the complete_with_tools path):
    tool_calls: list[ToolCall] | None = None  # role="assistant": tool calls the model requested
    tool_call_id: str | None = None  # role="tool": the ToolCall.id this message answers


def serialize_messages(messages: list[Message]) -> list[dict]:
    """Messages → JSON-safe dicts (so a paused run's transcript can be stored).

    str content stays a str; list content becomes ``[{"type": "text", "text"} | {"type": "image", "data",
    "mime_type"}]``; tool calls and tool_call_id ride along. ``deserialize_messages`` inverts it.
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, str):
            content: str | list = m.content
        else:
            content = [
                {"type": "image", "data": part.data, "mime_type": part.mime_type}
                if isinstance(part, ImagePart)
                else {"type": "text", "text": str(part)}
                for part in m.content
            ]
        d: dict = {"role": m.role, "content": content}
        if m.tool_calls:
            d["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": dict(c.arguments or {})} for c in m.tool_calls]
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        out.append(d)
    return out


def deserialize_messages(data) -> list[Message]:
    out: list[Message] = []
    for d in data or []:
        if not isinstance(d, dict):
            continue
        raw = d.get("content", "")
        if isinstance(raw, list):
            content: str | list = [
                ImagePart(data=str(part.get("data") or ""), mime_type=str(part.get("mime_type") or "image/png"))
                if isinstance(part, dict) and part.get("type") == "image"
                else str(part.get("text", "") if isinstance(part, dict) else part)
                for part in raw
            ]
        else:
            content = "" if raw is None else str(raw)
        calls = d.get("tool_calls") or None
        out.append(
            Message(
                role=str(d.get("role") or "user"),
                content=content,
                tool_calls=[ToolCall(id=str(c.get("id")), name=str(c.get("name")), arguments=dict(c.get("arguments") or {})) for c in calls]
                if calls
                else None,
                tool_call_id=d.get("tool_call_id") or None,
            )
        )
    return out


@dataclass
class CompletionOptions:
    max_tokens: int | None = None
    # None = leave it to the model's default. Many models (reasoning ones) refuse anything else.
    temperature: float | None = None
    top_p: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    raw: dict = field(default_factory=dict)
    # Populated by complete_with_tools when the model asks to call tools instead of answering:
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None


# --------------------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------------------

CAPABILITIES = ("vision", "structured_output", "embeddings", "tool_calls", "model_pull")
"""The capability flags, as ``supports_<name>`` on the provider class."""


class AIProvider(ABC):
    """One vendor API. Marvin builds an instance per use from ``from_credentials``."""

    provider_type: ClassVar[str] = ""
    """The slug workspaces select it by and executions record (``openai``, ``anthropic``…)."""
    display_name: ClassVar[str] = ""
    credentials: ClassVar[tuple[Credential, ...]] = ()

    supports_vision: ClassVar[bool] = False
    supports_structured_output: ClassVar[bool] = False
    supports_embeddings: ClassVar[bool] = False
    supports_tool_calls: ClassVar[bool] = False
    supports_model_pull: ClassVar[bool] = False
    """Can download models on demand (Ollama's /api/pull). Hosted APIs can't."""

    self_hosted: ClassVar[bool] = False
    """Runs cost nothing per token (the operator pays for the hardware, not the call)."""
    prices: ClassVar[Mapping[str, ModelPrice]] = {}
    """Model id → price. A model that isn't here (and isn't a dated snapshot of one) has no estimate."""
    default_model: ClassVar[str | None] = None
    suggested_models: ClassVar[tuple[str, ...]] = ()
    """Chat models the AI settings' picker suggests; any other id the vendor accepts still works."""
    default_embedding_model: ClassVar[str | None] = None

    @classmethod
    def from_credentials(cls, values: Mapping[str, Any]) -> AIProvider:
        """Build the provider from its declared credentials (see ``read_credentials``)."""
        raise NotImplementedError(f"{cls.__name__} does not implement from_credentials")

    @classmethod
    def capabilities(cls) -> dict[str, bool]:
        return {name: bool(getattr(cls, f"supports_{name}", False)) for name in CAPABILITIES}

    @classmethod
    def price(cls, model_id: str) -> ModelPrice | None:
        return price_for(cls.prices, model_id)

    @classmethod
    def estimate_cost(cls, model_id: str, prompt_tokens: int, completion_tokens: int) -> float | None:
        """Estimated USD for a run; 0.0 when self-hosted; None when the model has no price (shown as
        "—", never as a misleading "Free")."""
        if cls.self_hosted:
            return 0.0
        price = cls.price(model_id)
        return price.cost(prompt_tokens, completion_tokens) if price else None

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """One embedding vector per input text. Providers with embeddings override."""
        raise NotImplementedError(f"{self.provider_type} does not support embeddings")

    def pull_model(self, name: str, on_progress=None) -> None:
        """Download a model into the provider; ``on_progress`` gets ``{status, completed, total}`` per
        update. Only local providers that host their own weights (Ollama) override this."""
        raise NotImplementedError(f"{self.provider_type} does not support pulling models")

    def complete_with_tools(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition],
        options: CompletionOptions | None = None,
        tool_choice: str = "auto",
    ) -> CompletionResult:
        """Run one tool-calling turn.

        The model either answers (result.content, result.tool_calls empty) or requests tool calls
        (result.tool_calls populated). The caller — the agent loop — runs each requested tool, appends
        an assistant Message carrying result.tool_calls, then one role="tool" Message per result
        (echoing ToolCall.id via tool_call_id), and calls again until no tool calls remain.
        ``tool_choice`` is "auto" (model decides), "required" (must call a tool), or "none". Providers
        with function/tool calling override this.
        """
        raise NotImplementedError(f"{self.provider_type} does not support tool calling")

    @abstractmethod
    def complete(self, messages: list[Message], model: str, options: CompletionOptions | None = None) -> CompletionResult:
        """Send a chat completion request and return the result."""

    @abstractmethod
    def complete_structured(self, messages: list[Message], model: str, output_schema: dict, options: CompletionOptions | None = None) -> dict:
        """Request structured (JSON) output conforming to output_schema."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Model ids available from this provider."""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Validate the connection and credentials; ``(success, message)``. Never raises."""

    def execute_operation(
        self,
        messages: list[Message],
        model: str,
        output_schema: dict,
        options: CompletionOptions | None = None,
    ) -> tuple[dict, CompletionResult]:
        """Run a structured-output operation; return ``(parsed_dict, result_with_usage)``.

        Default: ``complete()`` then parse JSON from the content (code fences stripped). Providers with
        native structured output override this to use it while still returning token usage.
        """
        result = self.complete(messages, model, options)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.content.strip())
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {"raw": result.content}
        return parsed, result


# --------------------------------------------------------------------------------------------------
# The plugin
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AIProviderPlugin:
    """What a ``marvin.ai_providers`` entry point exports (the object, or a callable returning it).

    ``slug`` is the provider's ``provider_type``. A plugin whose slug matches one of Marvin's built-in
    providers replaces it; two plugins can't share a slug.
    """

    slug: str
    name: str
    provider: type[AIProvider]
    description: str = ""

    def __post_init__(self) -> None:
        if not _SLUG.match(self.slug or ""):
            raise ValueError(f"invalid AI provider slug {self.slug!r} (lower-case letters, digits, '-', '_')")
        if not (isinstance(self.provider, type) and issubclass(self.provider, AIProvider)):
            raise TypeError(f"AI provider plugin {self.slug!r}: provider must be an AIProvider subclass")
        if self.provider.provider_type != self.slug:
            raise ValueError(f"AI provider plugin {self.slug!r}: its provider's provider_type is {self.provider.provider_type!r}")
        keys = [c.key for c in self.provider.credentials]
        if len(keys) != len(set(keys)):
            raise ValueError(f"AI provider plugin {self.slug!r}: a credential is declared twice ({', '.join(keys)})")
