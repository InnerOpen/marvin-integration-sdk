"""The storage plugin contract: where Marvin keeps uploaded assets, and where it writes backups.

A storage plugin is a site-wide package the platform operator installs. It exposes one
``StoragePlugin`` through a ``marvin.storage_providers`` entry point, and offers either or both of:

- an asset **provider** (``StorageProvider``): Marvin stores uploads through it and hands out their
  public URLs. The operator picks it with ``STORAGE_PROVIDER=<slug>``.
- a backup **target** (``BackupTarget``): Marvin's backup engine writes database dumps, the config
  archive and an asset mirror to it, and restores from it.

Marvin core ships the built-in ``local`` provider and ``local`` target; everything else, cloud SDKs
included, lives in plugins. Each class declares the settings it reads (``Setting``) and builds itself
from them in ``from_config``; Marvin reads the values from its environment and masks secrets wherever
it shows them.

Run the conformance kit (``marvin_integration_sdk.storage.testing``) against your classes: Marvin runs
the same kit against its built-ins.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, ClassVar

__all__ = [
    "ENTRY_POINT_GROUP",
    "BackupTarget",
    "Setting",
    "StorageConfigError",
    "StorageMetadata",
    "StoragePlugin",
    "StorageProvider",
    "TargetObject",
    "masked_config",
    "read_config",
]

ENTRY_POINT_GROUP = "marvin.storage_providers"
"""The entry-point group Marvin discovers storage plugins from."""

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
MASK = "****"


class StorageConfigError(ValueError):
    """A provider or target can't be built from its settings. The message never carries a secret."""


@dataclass(frozen=True)
class Setting:
    """One setting a provider or target reads, by its environment variable name."""

    env: str
    """The variable Marvin reads, e.g. ``STORAGE_S3_BUCKET``."""
    label: str = ""
    secret: bool = False
    """Masked wherever Marvin shows the value (admin pages, logs)."""
    required: bool = False
    default: str | None = None
    help: str = ""


def read_config(settings: Iterable[Setting], source: Mapping[str, Any]) -> dict[str, Any]:
    """The declared settings' values from ``source`` (an environment, or Marvin's settings), defaults
    filled in. Raises ``StorageConfigError`` naming every required setting that has no value."""
    config: dict[str, Any] = {}
    missing: list[str] = []
    for s in settings:
        value = source.get(s.env)
        if value is None or value == "":
            value = s.default
        if s.required and (value is None or value == ""):
            missing.append(s.env)
        config[s.env] = value
    if missing:
        raise StorageConfigError(f"missing setting(s): {', '.join(missing)}")
    return config


def masked_config(settings: Iterable[Setting], config: Mapping[str, Any]) -> dict[str, Any]:
    """``config`` safe to show: a secret that is set reads ``****``."""
    secret = {s.env for s in settings if s.secret}
    return {k: (MASK if k in secret and v not in (None, "") else v) for k, v in config.items()}


# --------------------------------------------------------------------------------------------------
# Asset storage
# --------------------------------------------------------------------------------------------------


@dataclass
class StorageMetadata:
    """Metadata about a stored file."""

    storage_key: str
    """Logical key for the file in storage."""
    size: int
    """File size in bytes."""
    content_type: str
    """MIME type of the file."""
    checksum: str | None = None
    """Optional content digest (hex); ``checksum_algorithm`` says which."""
    metadata: dict | None = None
    """Optional custom metadata."""
    checksum_algorithm: str | None = None
    """The ``hashlib`` name of ``checksum`` (e.g. ``sha256``), when there is one."""


class StorageProvider(ABC):
    """Where uploaded assets live. Keys are ``/``-separated relative paths, e.g.
    ``<workspace>/assets/2026/07/<uuid>-photo.jpg``; a provider must not reinterpret them."""

    slug: ClassVar[str] = ""
    """The name the operator selects it by (``STORAGE_PROVIDER``) and asset rows record."""
    settings: ClassVar[tuple[Setting, ...]] = ()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> StorageProvider:
        """Build the provider from its declared settings (see ``read_config``)."""
        raise NotImplementedError(f"{cls.__name__} does not implement from_config")

    @abstractmethod
    def put(self, storage_key: str, file_data: BinaryIO, content_type: str, metadata: dict | None = None) -> StorageMetadata:
        """Store a file under ``storage_key``, replacing any previous one."""

    @abstractmethod
    def get(self, storage_key: str) -> BinaryIO:
        """An open binary stream of the file. Raises ``FileNotFoundError`` if it doesn't exist."""

    @abstractmethod
    def delete(self, storage_key: str) -> bool:
        """Delete a file. True if it was deleted, False if it didn't exist (never raises for that)."""

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        """Whether a file exists."""

    @abstractmethod
    def get_public_url(self, storage_key: str) -> str:
        """A URL a browser can fetch the file from."""

    @abstractmethod
    def get_metadata(self, storage_key: str) -> StorageMetadata:
        """Metadata about a stored file. Raises ``FileNotFoundError`` if it doesn't exist."""

    @abstractmethod
    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        """Every stored key that starts with ``prefix``, in a stable (sorted) order. The backup engine
        mirrors assets from any provider with this."""

    def checksum(self, storage_key: str, algorithm: str = "sha256") -> str | None:
        """The file's ``hashlib`` digest (hex) in ``algorithm``, or None when the provider can't say
        without downloading the file. The backup engine compares it against a target's digest so
        unchanged files aren't copied again; None means it compares sizes only."""
        return None


# --------------------------------------------------------------------------------------------------
# Backup targets
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetObject:
    """One object in a backup target."""

    key: str
    size: int
    digest: str = ""
    """Content digest (hex) in ``algorithm``; empty when the target has none for this object."""
    algorithm: str = ""
    """The ``hashlib`` name of ``digest`` (``sha256``, ``md5``). Empty means ``digest`` is not a
    content hash (e.g. an S3 multipart ETag), so only the size can be compared."""
    metadata: Mapping[str, str] = field(default_factory=dict)
    """User metadata, keys lower-cased. ``head`` returns it; ``list`` may leave it empty."""


class BackupTarget(ABC):
    """Where the backup engine writes. Keys are ``/``-separated relative paths (``postgres/…``,
    ``config/…``, ``assets/…``); the engine adds any per-environment prefix itself."""

    slug: ClassVar[str] = ""
    settings: ClassVar[tuple[Setting, ...]] = ()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> BackupTarget:
        """Build the target from its declared settings (see ``read_config``)."""
        raise NotImplementedError(f"{cls.__name__} does not implement from_config")

    @abstractmethod
    def put_file(self, key: str, path: Path, metadata: Mapping[str, str] | None = None, content_type: str | None = None) -> None:
        """Upload the file at ``path`` as ``key``, replacing any previous object, with ``metadata``
        (lower-case string keys and values) stored alongside. A reader never sees a partial object."""

    @abstractmethod
    def get(self, key: str, dest: Path) -> dict[str, str]:
        """Download ``key`` to ``dest``; return its metadata (keys lower-cased). Raises
        ``FileNotFoundError`` if it doesn't exist."""

    @abstractmethod
    def list(self, prefix: str) -> dict[str, TargetObject]:
        """Every object whose key starts with ``prefix``."""

    @abstractmethod
    def delete(self, keys: Iterable[str]) -> None:
        """Delete these objects; keys that don't exist are ignored."""

    @abstractmethod
    def head(self, key: str) -> TargetObject | None:
        """One object's size, digest and metadata, or None if it doesn't exist."""

    def describe(self) -> str:
        """Where this target writes, for logs (e.g. ``s3://bucket``). Never a credential."""
        return self.slug or type(self).__name__


# --------------------------------------------------------------------------------------------------
# The plugin
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoragePlugin:
    """What a ``marvin.storage_providers`` entry point returns (the object, or a callable returning it).

    ``slug`` names both sides: ``STORAGE_PROVIDER=<slug>`` selects the provider, a backup target of
    type ``<slug>`` uses the target.
    """

    slug: str
    name: str
    provider: type[StorageProvider] | None = None
    target: type[BackupTarget] | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not _SLUG.match(self.slug or ""):
            raise ValueError(f"invalid storage plugin slug {self.slug!r} (lower-case letters, digits, '-', '_')")
        if self.provider is None and self.target is None:
            raise ValueError(f"storage plugin {self.slug!r} offers neither a provider nor a target")
        if self.provider is not None and not (isinstance(self.provider, type) and issubclass(self.provider, StorageProvider)):
            raise TypeError(f"storage plugin {self.slug!r}: provider must be a StorageProvider subclass")
        if self.target is not None and not (isinstance(self.target, type) and issubclass(self.target, BackupTarget)):
            raise TypeError(f"storage plugin {self.slug!r}: target must be a BackupTarget subclass")

    @property
    def settings(self) -> tuple[Setting, ...]:
        """Every setting either side reads, in declaration order, each once."""
        seen: dict[str, Setting] = {}
        for cls in (self.provider, self.target):
            for s in getattr(cls, "settings", ()) if cls else ():
                seen.setdefault(s.env, s)
        return tuple(seen.values())
