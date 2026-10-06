"""The storage contract: plugin declaration, settings, and the conformance kit run on the reference
in-memory implementations."""

import pytest

from marvin_integration_sdk.storage import (
    BackupTarget,
    Setting,
    StorageConfigError,
    StoragePlugin,
    StorageProvider,
    masked_config,
    read_config,
)
from marvin_integration_sdk.storage.memory import MemoryBackupTarget, MemoryStorageProvider
from marvin_integration_sdk.storage.testing import BackupTargetContract, StorageProviderContract


class TestMemoryProvider(StorageProviderContract):
    @pytest.fixture
    def provider(self):
        return MemoryStorageProvider()


class TestMemoryTarget(BackupTargetContract):
    @pytest.fixture
    def target(self):
        return MemoryBackupTarget()


class _Provider(MemoryStorageProvider):
    settings = (Setting("X_BUCKET", required=True), Setting("X_SECRET", secret=True), Setting("X_REGION", default="auto"))


class _Target(MemoryBackupTarget):
    settings = (Setting("X_BUCKET", required=True), Setting("X_PREFIX"))


def test_plugin_settings_are_the_union_of_both_sides_each_once():
    plugin = StoragePlugin(slug="x", name="X", provider=_Provider, target=_Target)
    assert [s.env for s in plugin.settings] == ["X_BUCKET", "X_SECRET", "X_REGION", "X_PREFIX"]


@pytest.mark.parametrize("slug", ["", "S3", "has space", "-lead", "x" * 41])
def test_plugin_refuses_a_bad_slug(slug):
    with pytest.raises(ValueError, match="invalid storage plugin slug"):
        StoragePlugin(slug=slug, name="X", provider=_Provider)


def test_plugin_must_offer_something():
    with pytest.raises(ValueError, match="neither a provider nor a target"):
        StoragePlugin(slug="x", name="X")


def test_plugin_checks_the_classes_it_is_given():
    with pytest.raises(TypeError, match="provider must be a StorageProvider"):
        StoragePlugin(slug="x", name="X", provider=MemoryBackupTarget)
    with pytest.raises(TypeError, match="target must be a BackupTarget"):
        StoragePlugin(slug="x", name="X", target=MemoryStorageProvider)


def test_read_config_fills_defaults_and_names_every_missing_required_setting():
    assert read_config(_Provider.settings, {"X_BUCKET": "b"}) == {"X_BUCKET": "b", "X_SECRET": None, "X_REGION": "auto"}
    with pytest.raises(StorageConfigError) as err:
        read_config((*_Provider.settings, Setting("X_KEY", required=True)), {"X_SECRET": "hunter2"})
    assert "X_BUCKET" in str(err.value) and "X_KEY" in str(err.value)
    assert "hunter2" not in str(err.value)


def test_read_config_treats_empty_as_unset():
    assert read_config(_Provider.settings, {"X_BUCKET": "b", "X_REGION": ""})["X_REGION"] == "auto"
    with pytest.raises(StorageConfigError):
        read_config(_Provider.settings, {"X_BUCKET": ""})


def test_masked_config_hides_set_secrets_only():
    shown = masked_config(_Provider.settings, {"X_BUCKET": "b", "X_SECRET": "hunter2", "X_REGION": "auto"})
    assert shown == {"X_BUCKET": "b", "X_SECRET": "****", "X_REGION": "auto"}
    assert masked_config(_Provider.settings, {"X_SECRET": None}) == {"X_SECRET": None}


def test_contract_classes_are_abstract():
    with pytest.raises(TypeError):
        StorageProvider()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        BackupTarget()  # type: ignore[abstract]


def test_from_config_must_be_implemented():
    class Bare(MemoryStorageProvider):
        from_config = StorageProvider.__dict__["from_config"]

    with pytest.raises(NotImplementedError):
        Bare.from_config({})
