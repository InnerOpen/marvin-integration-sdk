"""In-memory reference implementations of the storage contract.

They hold everything in a dict and pass the conformance kit, so they serve as the executable
reading of the contract and as fakes in tests (a stand-in remote provider, a scratch target).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

from . import BackupTarget, StorageMetadata, StorageProvider, TargetObject


class MemoryStorageProvider(StorageProvider):
    """Assets in a dict. ``public_base_url`` prefixes the keys it hands out as URLs."""

    slug = "memory"

    def __init__(self, public_base_url: str = "memory://assets") -> None:
        self.public_base_url = public_base_url.rstrip("/")
        self.objects: dict[str, tuple[bytes, str, dict | None]] = {}

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> MemoryStorageProvider:
        return cls()

    def put(self, storage_key: str, file_data: BinaryIO, content_type: str, metadata: dict | None = None) -> StorageMetadata:
        data = file_data.read()
        self.objects[storage_key] = (data, content_type, dict(metadata) if metadata else None)
        return StorageMetadata(storage_key, len(data), content_type, hashlib.sha256(data).hexdigest(), metadata, "sha256")

    def get(self, storage_key: str) -> BinaryIO:
        if storage_key not in self.objects:
            raise FileNotFoundError(f"File not found: {storage_key}")
        return BytesIO(self.objects[storage_key][0])

    def delete(self, storage_key: str) -> bool:
        return self.objects.pop(storage_key, None) is not None

    def exists(self, storage_key: str) -> bool:
        return storage_key in self.objects

    def get_public_url(self, storage_key: str) -> str:
        return f"{self.public_base_url}/{storage_key}"

    def get_metadata(self, storage_key: str) -> StorageMetadata:
        if storage_key not in self.objects:
            raise FileNotFoundError(f"File not found: {storage_key}")
        data, content_type, metadata = self.objects[storage_key]
        return StorageMetadata(storage_key, len(data), content_type, hashlib.sha256(data).hexdigest(), metadata, "sha256")

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        yield from sorted(k for k in self.objects if k.startswith(prefix))

    def checksum(self, storage_key: str, algorithm: str = "sha256") -> str | None:
        if storage_key not in self.objects:
            raise FileNotFoundError(f"File not found: {storage_key}")
        return hashlib.new(algorithm, self.objects[storage_key][0]).hexdigest()


class MemoryBackupTarget(BackupTarget):
    """Backup objects in a dict, with sha256 digests."""

    slug = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> MemoryBackupTarget:
        return cls()

    def _object(self, key: str) -> TargetObject:
        data, metadata = self.objects[key]
        return TargetObject(key, len(data), hashlib.sha256(data).hexdigest(), "sha256", dict(metadata))

    def put_file(self, key: str, path: Path, metadata: Mapping[str, str] | None = None, content_type: str | None = None) -> None:
        self.objects[key] = (Path(path).read_bytes(), {k.lower(): str(v) for k, v in (metadata or {}).items()})

    def get(self, key: str, dest: Path) -> dict[str, str]:
        if key not in self.objects:
            raise FileNotFoundError(f"no such object: {key}")
        data, metadata = self.objects[key]
        Path(dest).write_bytes(data)
        return dict(metadata)

    def list(self, prefix: str) -> dict[str, TargetObject]:
        return {k: self._object(k) for k in sorted(self.objects) if k.startswith(prefix)}

    def delete(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)

    def head(self, key: str) -> TargetObject | None:
        return self._object(key) if key in self.objects else None

    def describe(self) -> str:
        return "memory"
