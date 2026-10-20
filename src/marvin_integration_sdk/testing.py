"""Conformance kit for integration plugins (needs pytest).

Subclass the contract in your plugin's tests and supply a fixture that returns the provider; every ``test_*``
method then runs against it:

    from marvin_integration_sdk.testing import IntegrationContract

    class TestSlackContract(IntegrationContract):
        @pytest.fixture
        def provider(self):
            return SlackProvider()

The class name doesn't start with ``Test``, so pytest only collects your subclass. It checks what Marvin relies on
without Marvin: the catalog entry serialises, keys are unique, every recipe passes ``check_recipe``, and a declared
guide loads. Marvin validates recipe definitions against its workflow model on top of this.
"""

from __future__ import annotations

import json

import pytest

from .base import hands_off_problems
from .recipes import MAX_GUIDE_BYTES, check_recipe, load_guide

__all__ = ["IntegrationContract"]


class IntegrationContract:
    """What every ``IntegrationProvider`` must satisfy."""

    @pytest.fixture
    def provider(self):
        raise NotImplementedError("override the `provider` fixture to return the provider instance")

    def test_identity(self, provider):
        assert provider.slug and provider.slug == provider.slug.strip().lower(), "slug: lower-case, no spaces"
        assert provider.name.strip(), "name is required"

    def test_info_is_json(self, provider):
        info = provider.info()
        json.dumps(info)  # what Marvin projects to its API and UI
        assert info["slug"] == provider.slug

    def test_keys_are_unique(self, provider):
        for kind, keys in (
            ("action", [a.key for a in provider.actions]),
            ("event", [e.key for e in provider.emits]),
            ("credential", [c.key for c in provider.credentials]),
            ("blueprint", [c.slug for c in provider.content]),
            ("recipe", [r.id for r in provider.recipes]),
        ):
            assert len(keys) == len(set(keys)), f"duplicate {kind} keys: {keys}"

    def test_recipes_are_well_formed(self, provider):
        problems = {r.id: check_recipe(r, provider) for r in provider.recipes}
        assert not any(problems.values()), {rid: p for rid, p in problems.items() if p}

    def test_hands_off_is_well_formed(self, provider):
        problems = [p for a in provider.actions for p in hands_off_problems(a)]
        assert not problems, problems

    def test_guide_loads(self, provider):
        if not getattr(provider, "guide", ""):
            pytest.skip("no guide declared")
        text = load_guide(provider)
        assert text is not None, f"guide {provider.guide!r} is missing, not .md, not UTF-8 or over {MAX_GUIDE_BYTES} bytes"
        assert text.lstrip().startswith("# "), "a guide starts with a '# Title' heading"
