"""Read a provider's logo from its package data.

A provider names its logo with ``IntegrationProvider.logo`` — a path relative to the package the
provider class lives in (``"logo.svg"``) — and ships the file as package data. ``load_logo`` reads it
through ``importlib.resources``, so it works from a wheel, a zip or an editable checkout alike.

The SDK only finds and reads the file. Whatever renders it (Marvin core) is the security boundary
and must validate the bytes before serving them.
"""

from __future__ import annotations

import sys
from importlib import resources
from pathlib import PurePosixPath

LOGO_CONTENT_TYPES = {".svg": "image/svg+xml", ".png": "image/png"}


def _package_of(provider) -> str | None:
    cls = provider if isinstance(provider, type) else type(provider)
    module = sys.modules.get(cls.__module__)
    package = getattr(module, "__package__", None) if module is not None else None
    if package:
        return package
    # A top-level module (no package) has nothing to read package data from.
    return cls.__module__.rpartition(".")[0] or None


def load_logo(provider) -> tuple[bytes, str] | None:
    """``(bytes, content_type)`` for the provider's declared logo, or None when it declares none, the
    file is missing, unreadable, outside its package, or not ``.svg``/``.png``. Accepts the provider
    class or an instance. Never raises."""
    logo = getattr(provider, "logo", "") or ""
    if not isinstance(logo, str) or not logo.strip():
        return None
    path = PurePosixPath(logo.strip())
    content_type = LOGO_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None or path.is_absolute() or ".." in path.parts or "\\" in logo:
        return None
    package = _package_of(provider)
    if package is None:
        return None
    try:
        resource = resources.files(package).joinpath(*path.parts)
        if not resource.is_file():
            return None
        return resource.read_bytes(), content_type
    except Exception:  # noqa: BLE001 — a broken logo must never break the provider
        return None
