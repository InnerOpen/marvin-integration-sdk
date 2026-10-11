"""What an integration teaches Marvin: recipes that pass check_recipe, a guide read from package data, both in
info(), and the conformance kit that holds a plugin to them."""

import importlib
import json
import textwrap

import pytest

from marvin_integration_sdk import (
    MAX_GUIDE_BYTES,
    IntegrationProvider,
    ProviderAction,
    WorkflowRecipe,
    check_recipe,
    load_guide,
)
from marvin_integration_sdk.testing import IntegrationContract

ANNOUNCE = WorkflowRecipe(
    id="announce-entry-type",
    title="Announce published entries",
    outcome="A published entry of one type is posted to the channel.",
    definition={
        "trigger": {"type": "event", "event": "entry_published"},
        "conditions": [{"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"}],
        "actions": [{"kind": "integration", "integration": "{{connection}}", "action": "send", "args": {"text": "New: ${entry.title}"}}],
    },
    variables={
        "entry_type": {"type": "entry_type_slug", "description": "Which entries.", "example": "post"},
        "connection": {"type": "integration_slug", "description": "The connection.", "example": "chat"},
    },
)


class Chat(IntegrationProvider):
    slug = "chat"
    name = "Chat"
    actions = (ProviderAction(key="send", label="Send"),)
    recipes = (ANNOUNCE,)


def _with(**changes) -> WorkflowRecipe:
    return WorkflowRecipe(**{**ANNOUNCE.__dict__, **changes})


# ── check_recipe ─────────────────────────────────────────────────────────────


def test_a_well_formed_recipe_has_no_problems():
    assert check_recipe(ANNOUNCE, Chat()) == []


@pytest.mark.parametrize(
    "recipe, problem",
    [
        (_with(id="Announce_Entries"), "kebab-case"),
        (_with(outcome=" "), "title and outcome"),
        (_with(definition={"trigger": {"type": "manual"}, "actions": []}), "at least one action"),
        (_with(variables={"entry_type": ANNOUNCE.variables["entry_type"]}), "{{connection}} is not declared"),
        (
            _with(variables={**ANNOUNCE.variables, "spare": {"type": "text", "description": "x", "example": "y"}}),
            "'spare' is declared but never used",
        ),
        (_with(variables={**ANNOUNCE.variables, "entry_type": {"type": "slug", "description": "x", "example": "y"}}), "has type 'slug'"),
        (_with(variables={**ANNOUNCE.variables, "entry_type": {"type": "entry_type_slug", "example": "post"}}), "needs a description"),
        (_with(variables={**ANNOUNCE.variables, "entry_type": {"type": "entry_type_slug", "description": "x"}}), "needs an example"),
    ],
)
def test_a_malformed_recipe_says_what_is_wrong(recipe, problem):
    assert any(problem in p for p in check_recipe(recipe)), check_recipe(recipe)


def test_a_recipe_may_only_call_its_own_providers_actions():
    foreign = _with(
        definition={**ANNOUNCE.definition, "on_failure": [{"kind": "integration", "integration": "{{connection}}", "action": "page_oncall"}]}
    )
    assert check_recipe(foreign) == []  # without the provider there's nothing to compare against
    assert check_recipe(foreign, Chat()) == ["integration step calls 'page_oncall', which this provider doesn't declare"]


# ── guide + info ─────────────────────────────────────────────────────────────


@pytest.fixture
def provider_with_guide(tmp_path, monkeypatch):
    """An importable package `<name>` whose provider declares `guide`, with the given files beside it."""
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(name: str, guide: str, files: dict[str, bytes]):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            textwrap.dedent(
                f"""
                from marvin_integration_sdk import IntegrationProvider

                class Provider(IntegrationProvider):
                    slug = "{name}"
                    name = "{name}"
                    guide = {guide!r}
                """
            )
        )
        for rel, data in files.items():
            (pkg / rel).write_bytes(data)
        return importlib.import_module(name).Provider

    return make


def test_the_guide_is_read_from_the_package(provider_with_guide):
    cls = provider_with_guide("guided_a", "guide.md", {"guide.md": "# Chat\n\nPaste the token.\n".encode()})
    assert load_guide(cls) == "# Chat\n\nPaste the token.\n"
    assert cls().info()["has_guide"] is True


@pytest.mark.parametrize(
    "guide, files",
    [
        ("", {}),
        ("missing.md", {}),
        ("guide.txt", {"guide.txt": b"# x"}),
        ("../guide.md", {}),
        ("guide.md", {"guide.md": b"# big\n" + b"x" * MAX_GUIDE_BYTES}),
        ("guide.md", {"guide.md": b"# \xff\xfe"}),
    ],
)
def test_a_bad_guide_is_none_never_an_error(provider_with_guide, guide, files):
    name = f"guided_{abs(hash((guide, tuple(files)))) % 10**8}"
    assert load_guide(provider_with_guide(name, guide, files)) is None


def test_info_lists_recipes_as_plain_data():
    info = Chat().info()
    assert json.loads(json.dumps(info["recipes"]))[0]["id"] == "announce-entry-type"
    assert info["has_guide"] is False


# ── the conformance kit, run against the provider above ──────────────────────


class TestChatContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return Chat()
