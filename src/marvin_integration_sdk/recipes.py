"""What an integration teaches Marvin about itself: Workflow Library recipes and a how-to guide.

**Recipes.** A provider lists ``WorkflowRecipe``s in ``IntegrationProvider.recipes``. Marvin shows each in its
Workflow Library, tagged with the plugin it came from, and offers it to a workspace once this integration is
connected there. A recipe is a whole workflow definition in Marvin's own format with ``{{lower_case}}`` setup
placeholders where it needs one of the workspace's own names (an entry type, the integration connection's slug, a
field); each placeholder is declared in ``variables`` with a type — which picker Marvin shows — a description and an
example. ``${…}`` run-time templates are left alone. Configuring one never saves; the person saves it, switched off.

**Guide.** ``IntegrationProvider.guide`` names a markdown file in the provider's package (``"guide.md"``, shipped as
package data, like ``logo``): how to set the integration up and use it well — what to paste where, what it can't
do. Marvin gives it to its agents when they build workflows for a workspace where the integration is connected, and
its manual search (``search_docs``) finds it. Keep it short (at most ``MAX_GUIDE_BYTES``), start it with a
``# Title``, and write it for a person: the agents read the same text.

``check_recipe`` is the structural check every recipe must pass (the conformance kit runs it, and Marvin refuses a
recipe that fails it); Marvin also validates the definition itself against its workflow model before showing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

from .logo import _package_of

PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
RECIPE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_GUIDE_BYTES = 16 * 1024

# Which picker a setup variable gets in Marvin. The slug/name/key types are the workspace's own things; the rest
# are plain values.
VARIABLE_TYPES = frozenset(
    {
        "entry_type_slug",
        "field_key",
        "collection_slug",
        "collection_name",
        "integration_slug",
        "webhook_id",
        "incoming_webhook_slug",
        "status",
        "text",
        "string",
        "integer",
        "number",
        "boolean",
        "object",
        "list",
    }
)


@dataclass(frozen=True)
class WorkflowRecipe:
    """A ready-made workflow for this integration, offered in Marvin's Workflow Library.

    ``id`` is kebab-case and unique within the provider (Marvin shows it as ``<provider slug>.<id>``).
    ``definition`` is the workflow definition (``trigger``, ``conditions``, ``actions``) with ``{{placeholders}}``.
    ``variables`` declares each placeholder: ``{"name": {"type": "entry_type_slug", "description": "…", "example": "post"}}``.
    """

    id: str
    title: str
    outcome: str  # one sentence: what happens when it runs
    definition: dict
    variables: dict[str, dict] = field(default_factory=dict)
    category: str = ""  # defaults to the provider's name in the Library
    tags: tuple[str, ...] = ()
    side_effects: tuple[dict, ...] = ()  # {"kind": "external_post" | "content_mutation" | "email" | …, "description": "…"}


def placeholders(node: Any) -> set[str]:
    """Every ``{{name}}`` in the definition, in values and keys."""
    found: set[str] = set()
    if isinstance(node, str):
        found.update(PLACEHOLDER.findall(node))
    elif isinstance(node, dict):
        for key, value in node.items():
            found |= placeholders(key) | placeholders(value)
    elif isinstance(node, list):
        for item in node:
            found |= placeholders(item)
    return found


def check_recipe(recipe: WorkflowRecipe, provider: Any = None) -> list[str]:
    """What is wrong with a recipe's shape — empty when Marvin can take it. With ``provider``, its integration steps
    may only call that provider's own actions."""
    problems: list[str] = []
    if not RECIPE_ID.match(recipe.id or ""):
        problems.append(f"id '{recipe.id}' must be kebab-case (a-z, 0-9, single hyphens)")
    if not (recipe.title or "").strip() or not (recipe.outcome or "").strip():
        problems.append("title and outcome are required")
    definition = recipe.definition
    if not isinstance(definition, dict) or not isinstance(definition.get("trigger"), dict) or not definition.get("actions"):
        return [*problems, "definition must be a workflow definition with a trigger and at least one action"]
    used, declared = placeholders(definition), set(recipe.variables)
    problems += [f"placeholder {{{{{name}}}}} is not declared in variables" for name in sorted(used - declared)]
    problems += [f"variable '{name}' is declared but never used" for name in sorted(declared - used)]
    for name, spec in recipe.variables.items():
        if not isinstance(spec, dict):
            problems.append(f"variable '{name}' must be an object with type, description and example")
            continue
        if spec.get("type") not in VARIABLE_TYPES:
            problems.append(f"variable '{name}' has type {spec.get('type')!r}; one of {', '.join(sorted(VARIABLE_TYPES))}")
        if not str(spec.get("description") or "").strip():
            problems.append(f"variable '{name}' needs a description (the person reads it when filling the recipe in)")
        if "example" not in spec:
            problems.append(f"variable '{name}' needs an example (Marvin validates the recipe filled with the examples)")
    if provider is not None:
        own = {a.key for a in getattr(provider, "actions", ())}
        for step in [*(definition.get("actions") or []), *(definition.get("on_failure") or [])]:
            if isinstance(step, dict) and step.get("kind") == "integration" and step.get("action") and step["action"] not in own:
                problems.append(f"integration step calls '{step['action']}', which this provider doesn't declare")
    return problems


def load_guide(provider) -> str | None:
    """The provider's guide as markdown, or None when it declares none, the file is missing, outside its package,
    not ``.md``, not UTF-8 or larger than ``MAX_GUIDE_BYTES``. Accepts the class or an instance. Never raises."""
    guide = getattr(provider, "guide", "") or ""
    if not isinstance(guide, str) or not guide.strip():
        return None
    path = PurePosixPath(guide.strip())
    if path.suffix.lower() != ".md" or path.is_absolute() or ".." in path.parts or "\\" in guide:
        return None
    package = _package_of(provider)
    if package is None:
        return None
    try:
        resource = resources.files(package).joinpath(*path.parts)
        if not resource.is_file():
            return None
        data = resource.read_bytes()
        if len(data) > MAX_GUIDE_BYTES:
            return None
        return data.decode("utf-8")
    except Exception:  # noqa: BLE001 — a broken guide must never break the provider
        return None
