"""The content declaration: a provider says what workspace content it needs, and creates nothing."""

from dataclasses import FrozenInstanceError

import pytest

from marvin_integration_sdk import ContentBlueprint, IntegrationProvider


def test_a_provider_declares_content_and_it_reaches_info():
    blueprint = ContentBlueprint(
        kind="entry_type",
        slug="thing-log",
        name="Thing log",
        description="One row per thing done.",
        payload={"name": "Thing log", "schema_json": {"fields": []}},
    )

    class _P(IntegrationProvider):
        slug = "prov"
        name = "Prov"
        content = (blueprint,)

    projected = _P().info()["content"]
    assert projected[0]["slug"] == "thing-log"
    assert projected[0]["payload"]["schema_json"] == {"fields": []}
    # category left to the core, which files it under the provider
    assert projected[0]["category"] is None


def test_declaring_nothing_is_the_default():
    class _P(IntegrationProvider):
        slug = "bare"
        name = "Bare"

    assert _P().content == ()
    assert _P().info()["content"] == []


def test_a_declaration_is_immutable():
    # Providers are instantiated once into a shared registry; a mutable declaration would let one
    # workspace's apply leak into another's.
    blueprint = ContentBlueprint(kind="collection", slug="c", name="C")
    with pytest.raises(FrozenInstanceError):
        blueprint.slug = "something-else"


def test_requirements_and_parameters_are_carried_through():
    blueprint = ContentBlueprint(
        kind="collection",
        slug="all-things",
        name="All things",
        requires=("entry_type:thing-log",),
        parameters=({"key": "entry_type", "label": "Which type?", "kind": "entry_type"},),
    )
    assert blueprint.requires == ("entry_type:thing-log",)
    assert blueprint.parameters[0]["kind"] == "entry_type"
