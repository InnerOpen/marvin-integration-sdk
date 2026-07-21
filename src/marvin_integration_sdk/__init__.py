"""Marvin Integration SDK — the contract for building integration providers.

Depend on this (not on ``marvin`` core) to write an integration. Register a provider by subclassing
``IntegrationProvider`` and decorating it with ``@register_provider``, then expose it via a
``marvin.integrations`` entry point in your package's pyproject.
"""

from .base import (
    CATEGORIES,
    CATEGORY_CAPABILITY,
    CATEGORY_DESTINATION,
    CATEGORY_NOTIFY,
    CATEGORY_SOURCE,
    INTEGRATION_REGISTRY,
    CredentialField,
    IntegrationContext,
    IntegrationProvider,
    PolledEvent,
    ProviderAction,
    ProviderEvent,
    get_provider,
    list_providers,
    register_provider,
)
from .http import HttpHelper, Response

__all__ = [
    "CATEGORIES",
    "CATEGORY_SOURCE",
    "CATEGORY_DESTINATION",
    "CATEGORY_CAPABILITY",
    "CATEGORY_NOTIFY",
    "CredentialField",
    "ProviderEvent",
    "ProviderAction",
    "PolledEvent",
    "IntegrationContext",
    "IntegrationProvider",
    "INTEGRATION_REGISTRY",
    "register_provider",
    "get_provider",
    "list_providers",
    "HttpHelper",
    "Response",
]

__version__ = "0.1.0"
