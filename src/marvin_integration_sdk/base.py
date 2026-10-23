"""
The Marvin integration contract.

A *provider* is a manifest — the credentials it needs, the config it takes, the actions it exposes,
and the events it emits — plus handlers. Crucially, a provider is **pure with respect to Marvin**:
it receives ``config``, the resolved ``secret``, a ``logger``, and an ``http`` helper, and it
**returns** results/events. It never touches the database or the event bus — the core owns all
persistence and dispatch. That keeps plugins decoupled from core internals and small to depend on.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from abc import ABC
from dataclasses import asdict, dataclass, field
from logging import Logger
from typing import ClassVar

from .errors import ErrorPolicy, policy_info, validate_policy
from .http import HttpHelper
from .logo import load_logo
from .recipes import WorkflowRecipe, load_guide

# Provider categories — drive UI grouping and answer "which way does data flow?".
CATEGORY_SOURCE = "source"  # pulls external content in (RSS, cloud storage, git)
CATEGORY_DESTINATION = "destination"  # pushes out on events (deploy hook, search index)
CATEGORY_CAPABILITY = "capability"  # provides tools / AI capability
CATEGORY_NOTIFY = "notify"  # sends notifications (Slack, Discord)
CATEGORIES = (CATEGORY_SOURCE, CATEGORY_DESTINATION, CATEGORY_CAPABILITY, CATEGORY_NOTIFY)

# The kinds of Social inbox item Marvin keeps (0.18.0). A `social.comments` read returns the first three; a provider's
# `inbox_items_from_webhook` may return any of them. Marvin's own words, not a network's.
INBOX_KINDS = (
    "comment",  # on the account's post
    "reply",  # in a comment thread
    "mention",  # someone @-mentioned the account
    "message",  # a direct message to the account: text, a shared post, a picture
    "story_reply",  # someone replied to the account's story
    "story_mention",  # someone mentioned the account in their story
    "reaction",  # someone reacted to the account's message
    "live_comment",  # on the account's live video
)


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


# How a hand-off ends, in Marvin's words. A provider maps whatever its service says onto these (see ``HandsOff``).
RESULT_IN_FLIGHT = "in_flight"  # sent, and the other side hasn't finished (or hasn't said so yet)
RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"
RESULT_STATUSES = (RESULT_IN_FLIGHT, RESULT_SUCCEEDED, RESULT_FAILED)


@dataclass(frozen=True)
class HandsOff:
    """Declares that an action hands work to another system, so Marvin can follow it up.

    The action returns once the work is *started*; whether it worked is only known later, when the other
    side reports back. A hands-off action tells the core where to find the run in its result, and the core
    keeps one record of the hand-off (its status, the link to the run, the message) that people see under
    Settings > Automation > Results. Nothing for the workflow author to remember: Marvin records it in
    flight when the step succeeds and as failed when the call itself fails. The reply is reported with the
    ``automation_result`` workflow step, which names the connection and the status in the provider's words.
    """

    remote_id: str = ""
    """The key in the action's result that holds the run's id on the other side (``execution_id``). Marvin
    settles the record by it when the reply names it. Leave empty when the service gives no id."""
    remote_url: str = ""
    """The key in the action's result that holds a link to the run (``execution_url``), shown as "Open the run"."""
    target: str = ""
    """The key in the action's *args* that names what was asked of the other side (an n8n workflow's path), shown
    as the record's target. Leave empty when the action has one fixed target."""
    statuses: dict[str, str] = field(default_factory=dict)
    """The service's words for how a run is going, to Marvin's: ``{"success": "succeeded", "error": "failed",
    "waiting": "in_flight"}``. Marvin's three (``in_flight``, ``succeeded``, ``failed``) need no entry."""
    record_if: str = ""
    """The key in the action's result that must be truthy for the hand-off to be recorded (0.14.0). For an action that
    sometimes hands work off and sometimes doesn't (a newsletter email that is sent, or only saved as a draft): it
    returns ``sent: True`` only when it started the work. Empty = every successful call is a hand-off. A call that
    fails has no result to read, so it records nothing either: its failure reaches people through the error policy."""


def hands_off_problems(action: ProviderAction) -> list[str]:
    """What is wrong with an action's ``hands_off`` declaration (empty when it is fine or absent)."""
    h = action.hands_off
    if h is None:
        return []
    if not isinstance(h, HandsOff):
        return [f"action '{action.key}': hands_off must be a HandsOff"]
    problems = []
    for word, status in h.statuses.items():
        if not isinstance(word, str) or not word.strip():
            problems.append(f"action '{action.key}': a status word must be a non-empty string, got {word!r}")
        if status not in RESULT_STATUSES:
            problems.append(f"action '{action.key}': status {word!r} maps to {status!r}, which is not one of {', '.join(RESULT_STATUSES)}")
    # The keys can only be checked against a schema the action actually declares.
    for label, key, schema in (
        ("remote_id", h.remote_id, action.output_schema),
        ("remote_url", h.remote_url, action.output_schema),
        ("record_if", h.record_if, action.output_schema),
        ("target", h.target, action.input_schema),
    ):
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if key and properties and key not in properties:
            where = "input_schema" if label == "target" else "output_schema"
            problems.append(f"action '{action.key}': hands_off.{label} {key!r} is not a property of its {where}")
    # A record_if with no declared output keys to read it from can never be checked — and a typo would silently record nothing.
    if h.record_if:
        properties = action.output_schema.get("properties") if isinstance(action.output_schema, dict) else None
        if not properties:
            problems.append(f"action '{action.key}': hands_off.record_if {h.record_if!r} needs the action's output_schema to declare it")
    return problems


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

    error_policy: ErrorPolicy = field(default_factory=dict)  # code -> Handle; overrides the provider's policy for this action
    hands_off: HandsOff | None = None  # set when the action starts work elsewhere that reports back later (0.13.0; record_if 0.14.0)


@dataclass(frozen=True)
class ContentBlueprint:
    """Workspace content this integration needs, described rather than created.

    A provider may never touch the database, so it *declares* what it requires — the entry types
    its actions read and write, collections that give that content a home, the scheduled tasks that
    drive it — and the core offers the list for review. Nothing is created on install: the
    workspace applies it, and applying creates only what is missing, so a customised copy survives
    an upgrade of this provider.

    A provider legitimately owns the names of its own content (an Instagram log entry type is
    Instagram's business). Ask for anything belonging to the *workspace* through ``parameters``.

    Mark ``required=True`` sparingly. A provider that declares its preferences as requirements is
    deciding how someone else's workspace should be run.
    """

    kind: str
    """What it builds: "entry_type" | "entry_fields" (fields added to an existing entry type) |
    "collection" | "scheduled_task" | "event_subscription" | "incoming_webhook" | "workflow" | "playbook" (0.15.0: build it
    with ``playbook_blueprint``; it arrives as a draft for someone to publish).
    Webhooks, workflows, tasks and subscriptions are always created switched off."""
    slug: str
    name: str
    description: str = ""
    required: bool = False
    """True only when the integration genuinely cannot work without it — an entry type an action
    reads or writes. Defaults to False, so declaring something required is a deliberate claim.

    Everything else is a *suggestion*: a worked example of how this integration could be used, which
    the workspace may want or not. "Post to Slack when an entry is published" is an editorial
    decision, not a requirement — Slack works fine without it."""
    category: str | None = None  # defaults to the provider's name, so its content groups together
    requires: tuple[str, ...] = ()  # "entry_type:<slug>" — within this provider's own bundle
    parameters: tuple[dict, ...] = ()
    payload: dict = field(default_factory=dict)


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
    resume: dict | None = None
    """The last ``IntegrationError.partial`` saved for this entry + action, so a retry continues where
    the failed attempt stopped. None on a first attempt."""
    idempotency_seed: str | None = None
    """Stable across one retry chain. Derive idempotency keys from it (see ``idempotency_key``) so a
    retried request the remote already processed isn't applied twice."""

    def idempotency_key(self, *parts) -> str:
        """A key that is the same on every attempt of this retry chain for the same ``parts``
        (e.g. ``ctx.idempotency_key("charge", order_id)``). Random when there is no seed."""
        if self.idempotency_seed is None:
            return uuid.uuid4().hex
        material = json.dumps([self.idempotency_seed, *map(str, parts)])
        return hashlib.sha256(material.encode()).hexdigest()[:32]


class IntegrationProvider(ABC):
    """A named external-service provider. Override the lifecycle hooks you support."""

    slug: str  # registry key, e.g. "vercel_deploy"
    name: str  # human label, e.g. "Vercel Deploy Hook"
    description: str = ""
    category: str = CATEGORY_DESTINATION
    icon: str = ""
    """Optional emoji shown beside this provider in the UI. Marvin renders it as text, so an emoji
    works and an icon-set name or URL does not. Purely cosmetic — a provider without one is fine.
    It stays the fallback wherever a ``logo`` is missing or refused."""
    logo: ClassVar[str] = ""
    """Optional official logo: a path relative to this provider class's package (e.g. ``"logo.svg"``),
    shipped as package data. ``.svg`` or ``.png``. Read with ``load_logo``; Marvin validates it (size,
    no scripts or external references) and falls back to ``icon`` if it is refused."""
    config_schema: dict = {}  # JSON schema for `config`, validated on create/update
    credentials: tuple[CredentialField, ...] = ()
    emits: tuple[ProviderEvent, ...] = ()
    actions: tuple[ProviderAction, ...] = ()
    content: tuple[ContentBlueprint, ...] = ()
    """Workspace content this integration needs. Offered for review on install, never auto-applied."""
    system_playbooks: tuple[ContentBlueprint, ...] = ()
    """Playbooks every workspace gets automatically (0.16.0), read-only, updated with this package: build each with
    ``playbook_blueprint(__package__, "playbooks/<name>", scope="system")``. Kept apart from ``content`` so an older Marvin,
    which reads only ``content``, never offers one as an Add item. See ``marvin_integration_sdk.playbooks``."""
    recipes: tuple[WorkflowRecipe, ...] = ()
    """Ready-made workflows for this integration, shown in Marvin's Workflow Library (tagged with this plugin) and
    offered once it is connected. Unlike a ``workflow`` blueprint nothing is created: the person fills one in and
    saves it. See ``marvin_integration_sdk.recipes``."""
    guide: ClassVar[str] = ""
    """Optional how-to: a markdown file in this provider's package (``"guide.md"``, package data), at most
    ``MAX_GUIDE_BYTES``. Marvin's agents read it when building workflows where this integration is connected, and
    its manual search finds it. Read with ``load_guide``."""

    signature_schemes: dict[str, dict] = {}
    """Webhook signature schemes this integration's senders use, by name — offered to every incoming
    webhook alongside Marvin's core presets (a core name always wins). Each value describes the HMAC
    construction: ``algorithm`` (sha1|sha256|sha512), ``encoding`` (hex|base64), ``message`` (a template
    over ``{body}``, ``{url}``, ``{header:Name}``, ``{t}``; must include ``{body}``), ``header``, and
    optionally ``prefix``, ``multiple``, ``header_format`` (plain|stripe), ``key_format`` (raw|base64),
    ``timestamp_header``, ``tolerance_seconds``, ``notes``. E.g. Square:
    ``{"square": {"encoding": "base64", "message": "{url}{body}", "header": "x-square-hmacsha256-signature"}}``."""

    verification_schemes: dict[str, dict] = {}
    """How this integration's senders check a webhook URL before they deliver, by name — offered to every incoming
    webhook (Marvin's core has only ``custom``). The sender sends a GET carrying a challenge and the verify token it
    was given; when the token matches the webhook's secret, Marvin echoes the challenge back. Each value names the
    query parameters: ``challenge_param`` and ``token_param`` (both required), optionally ``mode_param`` with the
    ``mode_values`` it must take, ``content_type`` (default ``text/plain``) and ``notes``. E.g. Meta (WebSub):
    ``{"meta": {"challenge_param": "hub.challenge", "token_param": "hub.verify_token", "mode_param": "hub.mode",
    "mode_values": ["subscribe"]}}``."""

    error_policy: ClassVar[ErrorPolicy] = {}
    """How the core handles an ``IntegrationError`` by its ``code`` — e.g.
    ``{"rate_limited": Handle(retry=Retry(backoff=(60, 300))), "*": Handle(review=True)}``. An action's
    own ``error_policy`` takes precedence; see ``resolve_policy`` for the lookup order."""

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

    def inbox_items_from_webhook(self, payload: dict, ctx: IntegrationContext) -> list[dict]:
        """For social networks that push (0.18.0): a delivery to this integration's own incoming webhook (one its
        ``content`` blueprint created), turned into Social inbox items. Marvin calls it after the delivery's signature is
        verified and stores what it returns as the engagement poll's items are stored — deduplicated by ``id``, the
        account's own items skipped — so return the same shape ``social.comments`` does: ``id`` (the network's id for the
        item; the same one the poll gives, so it isn't stored twice), ``kind`` (one of ``INBOX_KINDS``), ``author``
        ({id, handle, name}), ``text``, ``created_at`` (ISO 8601, UTC), and optionally ``post_id``, ``post_url``, ``url``,
        ``network`` and ``attachments`` (a list of ``{type, url, title}``: a picture, a shared post and its caption).
        Return [] for anything that isn't an inbox item (read receipts, the account's own messages). Never call the
        network here: ``ctx`` carries the connection's config, not its secret. Default: nothing."""
        return []

    def result_status(self, word: str, action: str | None = None) -> str | None:
        """Marvin's status (``in_flight``, ``succeeded`` or ``failed``) for a word this service uses for how a
        hand-off is going, or None when the word is unknown. The default reads ``HandsOff.statuses`` (of the named
        action, else of every hands-off action) and accepts Marvin's own three; override it for a service whose
        words need more than a lookup."""
        key = str(word or "").strip().lower()
        if key in RESULT_STATUSES:
            return key
        for a in self.actions:
            if a.hands_off and (action is None or a.key == action):
                for service_word, status in a.hands_off.statuses.items():
                    if service_word.strip().lower() == key:
                        return status
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
            "icon": self.icon,
            "has_logo": load_logo(self) is not None,
            "config_schema": self.config_schema,
            "credentials": [asdict(c) for c in self.credentials],
            "emits": [asdict(e) for e in self.emits],
            "actions": [{**asdict(a), "error_policy": {code: h.to_dict() for code, h in a.error_policy.items()}} for a in self.actions],
            "content": [asdict(c) for c in self.content],
            "system_playbooks": [asdict(c) for c in self.system_playbooks],
            "recipes": [asdict(r) for r in self.recipes],
            "has_guide": load_guide(self) is not None,
            "signature_schemes": dict(self.signature_schemes),
            "verification_schemes": dict(self.verification_schemes),
            "error_policy": policy_info(self),
        }


# The registry every provider registers into and the core reads from. Shared across core and plugins
# because they all import this exact module from the SDK.
INTEGRATION_REGISTRY: dict[str, IntegrationProvider] = {}


def register_provider(cls):
    """Class decorator that adds the provider to the global registry. Rejects malformed error policies and hands_off declarations."""
    validate_policy(cls.error_policy, f"provider '{cls.slug}'")
    for action in cls.actions:
        validate_policy(action.error_policy, f"provider '{cls.slug}' action '{action.key}'")
        if problems := hands_off_problems(action):
            raise ValueError(f"provider '{cls.slug}': {problems[0]}")
    INTEGRATION_REGISTRY[cls.slug] = cls()
    return cls


def get_provider(slug: str) -> IntegrationProvider:
    if slug not in INTEGRATION_REGISTRY:
        raise KeyError(f"integration provider '{slug}' not found. Available: {list(INTEGRATION_REGISTRY)}")
    return INTEGRATION_REGISTRY[slug]


def list_providers() -> list[IntegrationProvider]:
    return list(INTEGRATION_REGISTRY.values())
