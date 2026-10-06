"""Conformance kit for storage plugins (needs pytest).

Subclass the contract classes in your plugin's tests and supply a fixture that returns a fresh,
empty instance; every ``test_*`` method then runs against it:

    from marvin_integration_sdk.storage.testing import BackupTargetContract, StorageProviderContract

    class TestS3Provider(StorageProviderContract):
        @pytest.fixture
        def provider(self, bucket):
            return S3StorageProvider(bucket=bucket, ...)

    class TestS3Target(BackupTargetContract):
        @pytest.fixture
        def target(self, bucket):
            return S3Target(bucket=bucket, ...)

The class names don't start with ``Test``, so pytest only collects your subclasses. Marvin runs the
same kit against its built-in ``local`` provider and target.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from . import BackupTarget, StorageProvider, TargetObject

__all__ = ["BackupTargetContract", "StorageProviderContract"]

PAYLOAD = b"marvin storage conformance\n" * 64


class StorageProviderContract:
    """What every ``StorageProvider`` must do."""

    @pytest.fixture
    def provider(self) -> StorageProvider:
        raise NotImplementedError("override the `provider` fixture to return a fresh, empty provider")

    def _put(self, provider: StorageProvider, key: str, data: bytes = PAYLOAD, content_type: str = "text/plain"):
        return provider.put(key, BytesIO(data), content_type)

    def test_round_trip(self, provider):
        meta = self._put(provider, "ws/assets/2026/07/a-photo.txt")
        assert meta.storage_key == "ws/assets/2026/07/a-photo.txt"
        assert meta.size == len(PAYLOAD)
        with provider.get("ws/assets/2026/07/a-photo.txt") as fh:
            assert fh.read() == PAYLOAD

    def test_put_replaces(self, provider):
        self._put(provider, "ws/k.txt", b"first")
        self._put(provider, "ws/k.txt", b"second")
        with provider.get("ws/k.txt") as fh:
            assert fh.read() == b"second"

    def test_missing_key_raises_file_not_found(self, provider):
        with pytest.raises(FileNotFoundError):
            provider.get("ws/nope.txt")
        with pytest.raises(FileNotFoundError):
            provider.get_metadata("ws/nope.txt")

    def test_exists(self, provider):
        assert provider.exists("ws/k.txt") is False
        self._put(provider, "ws/k.txt")
        assert provider.exists("ws/k.txt") is True

    def test_delete_is_idempotent(self, provider):
        self._put(provider, "ws/k.txt")
        assert provider.delete("ws/k.txt") is True
        assert provider.delete("ws/k.txt") is False
        assert provider.exists("ws/k.txt") is False

    def test_iter_keys_by_prefix(self, provider):
        for key in ("a/assets/2.txt", "a/assets/1.txt", "a/other.txt", "b/assets/1.txt"):
            self._put(provider, key)
        assert list(provider.iter_keys("a/assets/")) == ["a/assets/1.txt", "a/assets/2.txt"]
        assert sorted(provider.iter_keys("a/")) == ["a/assets/1.txt", "a/assets/2.txt", "a/other.txt"]
        assert sorted(provider.iter_keys()) == ["a/assets/1.txt", "a/assets/2.txt", "a/other.txt", "b/assets/1.txt"]
        assert list(provider.iter_keys("c/")) == []

    def test_metadata_reports_size_and_a_true_checksum(self, provider):
        self._put(provider, "ws/k.txt")
        meta = provider.get_metadata("ws/k.txt")
        assert meta.size == len(PAYLOAD)
        if meta.checksum and meta.checksum_algorithm:
            assert meta.checksum == hashlib.new(meta.checksum_algorithm, PAYLOAD).hexdigest()

    def test_checksum_is_true_or_none(self, provider):
        self._put(provider, "ws/k.txt")
        for algorithm in ("sha256", "md5"):
            value = provider.checksum("ws/k.txt", algorithm)
            assert value is None or value == hashlib.new(algorithm, PAYLOAD).hexdigest()

    def test_public_url_is_a_string(self, provider):
        self._put(provider, "ws/k.txt")
        url = provider.get_public_url("ws/k.txt")
        assert isinstance(url, str) and url


class BackupTargetContract:
    """What every ``BackupTarget`` must do."""

    @pytest.fixture
    def target(self) -> BackupTarget:
        raise NotImplementedError("override the `target` fixture to return a fresh, empty target")

    @pytest.fixture
    def source(self, tmp_path) -> Path:
        path = tmp_path / "source.bin"
        path.write_bytes(PAYLOAD)
        return path

    @staticmethod
    def _assert_digest(obj: TargetObject, data: bytes) -> None:
        if obj.algorithm:
            assert obj.digest == hashlib.new(obj.algorithm, data).hexdigest()

    def test_round_trip_keeps_metadata(self, target, source, tmp_path):
        target.put_file("config/marvin-config-20260101T000000Z.tar.gz", source, {"sha256": "abc", "db-sha256": "def"}, "application/gzip")
        dest = tmp_path / "out.bin"
        meta = target.get("config/marvin-config-20260101T000000Z.tar.gz", dest)
        assert dest.read_bytes() == PAYLOAD
        assert meta.get("sha256") == "abc"
        assert meta.get("db-sha256") == "def"

    def test_put_replaces(self, target, source, tmp_path):
        target.put_file("assets/ws/k.txt", source)
        newer = tmp_path / "newer.bin"
        newer.write_bytes(b"newer")
        target.put_file("assets/ws/k.txt", newer)
        dest = tmp_path / "out.bin"
        target.get("assets/ws/k.txt", dest)
        assert dest.read_bytes() == b"newer"
        assert target.head("assets/ws/k.txt").size == len(b"newer")

    def test_missing_key_raises_file_not_found(self, target, tmp_path):
        with pytest.raises(FileNotFoundError):
            target.get("postgres/nope.dump", tmp_path / "out")

    def test_head(self, target, source):
        assert target.head("postgres/marvin-20260101T000000Z.dump") is None
        target.put_file("postgres/marvin-20260101T000000Z.dump", source, {"sha256": "abc"})
        obj = target.head("postgres/marvin-20260101T000000Z.dump")
        assert obj is not None
        assert obj.key == "postgres/marvin-20260101T000000Z.dump"
        assert obj.size == len(PAYLOAD)
        assert obj.metadata.get("sha256") == "abc"
        self._assert_digest(obj, PAYLOAD)

    def test_list_by_prefix(self, target, source):
        for key in ("assets/ws/a.txt", "assets/ws/sub/b.txt", "config/marvin-config-20260101T000000Z.tar.gz", "assetsx/c.txt"):
            target.put_file(key, source)
        listed = target.list("assets/")
        assert sorted(listed) == ["assets/ws/a.txt", "assets/ws/sub/b.txt"]
        for key, obj in listed.items():
            assert obj.key == key
            assert obj.size == len(PAYLOAD)
            self._assert_digest(obj, PAYLOAD)
        assert sorted(target.list("")) == sorted(
            ["assets/ws/a.txt", "assets/ws/sub/b.txt", "config/marvin-config-20260101T000000Z.tar.gz", "assetsx/c.txt"]
        )
        assert target.list("sqlite/") == {}

    def test_delete_is_idempotent(self, target, source):
        target.put_file("sqlite/marvin-20260101T000000Z.db.gz", source)
        target.put_file("sqlite/marvin-20260102T000000Z.db.gz", source)
        target.delete(["sqlite/marvin-20260101T000000Z.db.gz", "sqlite/missing.db.gz"])
        assert sorted(target.list("sqlite/")) == ["sqlite/marvin-20260102T000000Z.db.gz"]
        target.delete(["sqlite/marvin-20260101T000000Z.db.gz"])
        target.delete([])

    def test_describe_is_a_string(self, target):
        assert isinstance(target.describe(), str) and target.describe()
