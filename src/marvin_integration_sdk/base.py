"""
The Marvin integration contract.

A *provider* is a manifest — the credentials it needs, the config it takes, the actions it exposes,
and the events it emits — plus handlers. Crucially, a provider is **pure with respect to Marvin**:
it receives ``config``, the resolved ``secret``, a ``logger``, and an ``http`` helper, and it
**returns** results/events. It never touches the database or the event bus — the core owns all
persistence and dispatch. That keeps plugins decoupled from core internals and small to depend on.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import asdict, dataclass, field
from logging import Logger

from .http import HttpHelper

# Provider categories — drive UI grouping and answer "which way does data flow?".
CATEGORY_SOURCE = "source"  # pulls external content in (RSS, cloud storage, git)
CATEGORY_DESTINATION = "destination"  # pushes out on events (deploy hook, search index)
CATEGORY_CAPABILITY = "capability"  # provides tools / AI capability
CATEGORY_NOTIFY = "notify"  # sends notifications (Slack, Discord)
CATEGORIES = (CATEGORY_SOURCE, CATEGORY_DESTINATION, CATEGORY_CAPABILITY, CATEGORY_NOTIFY)


@dataclass(frozen=True)
class CredentialField:
    """A secret this provider needs. Drives the create form + secret storage."""

    key: str
    label: str
    help: str = ""
    required: bool = True


@dataclass(frozen=True)
class ProviderEvent:
    """An inbound event this integration can emit onto the bus (polled or webhook-pushed)."""

    key: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class ProviderAction:
    """An outbound action this integration exposes to automation workflows.

    An action may also declare a **capability** — a standard kind (``image.generate``,
    ``image.describe``, …) that lets a capability resolver discover and invoke it uniformly,
    regardless of provider. Capability actions follow the canonical input/output dict shape for
    their kind; ``input_schema``/``output_schema`` document those keys.
    """

    key: str
    label: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    # --- capability routing (optional) ---
    capability: str | None = None  # e.g. "image.generate", "image.describe", "image.edit", "image.search", "image.upscale"
    output_schema: dict = field(default_factory=dict)  # JSON schema for the canonical outputs of this capability
    priority: int = 0  # higher wins when several providers advertise the same capability
    cost_hint: str | None = None  # "free" | "cheap" | "paid" | …  (advisory, for selection)
    requires_approval: bool = False  # generative/irreversible → resolver should gate via approval_mode


@dataclass
class PolledEvent:
    """An event a source provider produced. The provider returns these; the core dispatches them."""

    event_key: str  # matches a ProviderEvent.key declared in `emits`
    dedup_key: str  # stable id (feed guid, etc.) so the core can skip duplicates
    message: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class IntegrationContext:
    """
    Everything a provider handler is given — and nothing more.

    No database session, no event bus: a provider gets its config, its resolved secret, a logger,
    and a safe HTTP client, then returns. The core resolves secrets and dispatches returned events.
    """

    config: dict
    secret: str | None
    logger: Logger
    http: HttpHelper


class IntegrationProvider(ABC):
    """A named external-service provider. Override the lifecycle hooks you support."""

    slug: str  # registry key, e.g. "vercel_deploy"
    name: str  # human label, e.g. "Vercel Deploy Hook"
    description: str = ""
    category: str = CATEGORY_DESTINATION
    config_schema: dict = {}  # JSON schema for `config`, validated on create/update
    credentials: tuple[CredentialField, ...] = ()
    emits: tuple[ProviderEvent, ...] = ()
    actions: tuple[ProviderAction, ...] = ()

    # --- lifecycle (override what you support) ---

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        """Health probe → (status, error). Default: ok if a required credential is present."""
        if self.credentials and any(c.required for c in self.credentials) and not ctx.secret:
            return ("unconfigured", "Missing credential.")
        return ("ok", None)

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        """Run an action declared in `actions`. Returns a result dict."""
        raise NotImplementedError(f"{self.slug} does not implement actions")

    def poll(self, ctx: IntegrationContext) -> list[PolledEvent]:
        """For polled sources: return events to dispatch. The core dispatches them."""
        return []

    def on_webhook(self, payload: dict, ctx: IntegrationContext) -> PolledEvent | None:
        """For webhook-pushed sources: translate a raw payload into a bus event."""
        return None

    # --- helpers ---

    def get_action(self, key: str) -> ProviderAction | None:
        return next((a for a in self.actions if a.key == key), None)

    def info(self) -> dict:
        """Projected to the frontend (and optionally MarvinMCP) as the provider catalog entry."""
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "config_schema": self.config_schema,
            "credentials": [asdict(c) for c in self.credentials],
            "emits": [asdict(e) for e in self.emits],
            "actions": [asdict(a) for a in self.actions],
        }


# The registry every provider registers into and the core reads from. Shared across core and plugins
# because they all import this exact module from the SDK.
INTEGRATION_REGISTRY: dict[str, IntegrationProvider] = {}


def register_provider(cls):
    """Class decorator that adds the provider to the global registry."""
    INTEGRATION_REGISTRY[cls.slug] = cls()
    return cls


def get_provider(slug: str) -> IntegrationProvider:
    if slug not in INTEGRATION_REGISTRY:
        raise KeyError(f"integration provider '{slug}' not found. Available: {list(INTEGRATION_REGISTRY)}")
    return INTEGRATION_REGISTRY[slug]


def list_providers() -> list[IntegrationProvider]:
    return list(INTEGRATION_REGISTRY.values())
