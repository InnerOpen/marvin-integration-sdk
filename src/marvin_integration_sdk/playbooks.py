"""Playbooks an integration ships: written procedures Marvin's agents load, packaged as a skill folder.

An integration knows things a workspace doesn't: what makes a good newsletter issue, how to read a failed build. It
ships that as a skill folder in its package data (the Agent Skills format Marvin's Playbooks use)::

    marvin_integration_acme/playbooks/acme-weekly-report/SKILL.md
    marvin_integration_acme/playbooks/acme-weekly-report/references/checklist.md

and declares it with ``playbook_blueprint``::

    content = (playbook_blueprint(__package__, "playbooks/acme-weekly-report"),)

Marvin lists it on the integration's card under Optional. An admin adds it and it arrives as a **draft** (with the
import review) for someone to read and publish; after that it updates like a workflow does, and an edited copy is
never overwritten without asking.

Rules, checked here and again by Marvin (it never trusts this module):

* The Playbook's name (the ``name`` in the SKILL.md) starts with the provider's slug and a hyphen
  (``buttondown-newsletter-issue``; a slug's underscores count as hyphens: ``cloudflare_pages`` gives ``cloudflare-pages-``),
  so integrations can't take each other's names or a workspace's own.
* Text only (``.md .txt .json .csv .yaml``); no ``scripts/`` and no script extensions; relative paths without ``..``.
* ``SKILL.md`` at most ``SKILL_MAX_BYTES``; at most ``MAX_FILES`` other files of at most ``FILE_MAX_BYTES`` each and
  ``TOTAL_MAX_BYTES`` in all.
* ``allowed-tools`` names Marvin tools only (``list_events``); it narrows what an agent may use, never widens it.

A Playbook that drafts text should tell the agent to write it itself and save it with ``create_draft`` /
``update_draft``, not ``compose_entry`` / ``revise_entry`` (those hand the writing to an AI that never loads Playbooks).

**System Playbooks (0.16.0).** Pass ``scope="system"`` and list the blueprint in the provider's ``system_playbooks`` (or a
storage / AI plugin's ``playbooks``) and Marvin offers it in *every* workspace automatically, read-only, labelled with your
plugin's name, updated with your release. A workspace can switch it off or copy it to customise; an operator can switch it off
for the server. Use it for procedures nobody should have to install (how to read a failed Check connection), not for
suggestions. On top of the rules above (``check_playbook(..., scope="system")`` checks them, ``playbook_warnings`` adds the
soft ones):

1. A provider's ``guide.md`` stays the reference for building workflows; a skill covers doing a job and points to the guide
   (``read_doc plugins/<provider>.md``), never restating actions or recipes.
2. The manual explains what a feature is; a skill is a procedure (steps, checks, a good result) and links the page.
3. Workflow authoring stays with ``workflow_authoring_guide`` and the Builder: no "how to set up a workflow" skill.
4. Instructions say who, skills say how. A system skill never sets voice or tone (it says "if this workspace has a voice
   Playbook in your list, load it") and never says who the agent is.
5. Manual-only by default (no ``disable-model-invocation`` key means manual-only); ``false`` only for broadly useful ones.
6. ``allowed-tools`` is rarely right: it is allowed (narrowing is safety) but warned about.
7. Write it yourself: a drafting skill saves with ``create_draft`` / ``update_draft``. ``compose_entry`` / ``revise_entry`` in
   ``allowed-tools`` is refused; in the body it is warned about.
8. Slugs only: a UUID-looking token is refused (ids differ in every workspace).
"""

from __future__ import annotations

import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any

from .base import ContentBlueprint

SKILL_FILE = "SKILL.md"
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".json", ".csv", ".yaml"})
# Same list as Marvin's Playbook importer: a skill's scripts are never shipped or run.
SCRIPT_EXTENSIONS = frozenset({".sh", ".bash", ".zsh", ".fish", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd"})
SCRIPT_DIR = "scripts"
SKILL_MAX_BYTES = 64 * 1024
FILE_MAX_BYTES = 64 * 1024
MAX_FILES = 20
TOTAL_MAX_BYTES = 256 * 1024
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_REFERENCED = re.compile(r"(?<![\w/.-])((?:references|assets)/[A-Za-z0-9_./-]*[A-Za-z0-9_])")
_FENCE = re.compile(r"^---[ \t]*$", re.MULTILINE)
SCOPES = ("workspace", "system")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_AI_WRITERS = ("compose_entry", "revise_entry")
_NEGATION = re.compile(r"\b(not|never|don'?t|instead|no)\b", re.IGNORECASE)


def provider_prefix(slug: str) -> str:
    """What a provider's Playbook names start with: its slug with underscores as hyphens, then a hyphen (a Playbook name has no
    underscores, so ``cloudflare_pages`` gives ``cloudflare-pages-``). Empty for no slug."""
    return f"{slug.replace('_', '-')}-" if slug else ""


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    """Top-level ``key: value`` lines of the SKILL.md frontmatter (indented lines continue the value) and the body.
    The SDK has no YAML dependency; Marvin parses the real thing."""
    text = text.lstrip("﻿").replace("\r\n", "\n").lstrip()
    fences = list(_FENCE.finditer(text))
    if not text.startswith("---") or len(fences) < 2 or fences[0].start() != 0:
        return {}, text
    pairs: dict[str, str] = {}
    key = None
    for line in text[fences[0].end() : fences[1].start()].split("\n"):
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if match and not line[:1].isspace():
            key = match.group(1)
            pairs[key] = match.group(2).strip()
        elif key and line.strip() and line[:1].isspace():
            pairs[key] = f"{pairs[key]} {line.strip()}".strip()
    for k, v in pairs.items():
        if v in (">", "|", ">-", "|-"):
            pairs[k] = ""
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            pairs[k] = v[1:-1]
    return pairs, text[fences[1].end() :]


def _path_problem(path: str) -> str | None:
    p = PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts or "\\" in path:
        return f"'{path}' must be a relative path inside the folder, without '..'"
    if p.parts[0].lower() == SCRIPT_DIR or p.suffix.lower() in SCRIPT_EXTENSIONS:
        return f"'{path}' is a script; Playbooks ship text only"
    if p.suffix.lower() not in TEXT_EXTENSIONS:
        return f"'{path}' must be one of {', '.join(sorted(TEXT_EXTENSIONS))}"
    return None


def _walk(folder: Traversable, prefix: str = "") -> list[tuple[str, Traversable]]:
    found = []
    for child in sorted(folder.iterdir(), key=lambda c: c.name):
        path = f"{prefix}{child.name}"
        if child.name in {"__pycache__", ".DS_Store"} or child.name.startswith("."):
            continue
        if child.is_dir():
            found += _walk(child, f"{path}/")
        else:
            found.append((path, child))
    return found


def _title(body: str, name: str) -> str:
    for line in body.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return name.replace("-", " ").capitalize()


def playbook_blueprint(package: str, folder: str, *, title: str | None = None, scope: str = "workspace") -> ContentBlueprint:
    """Read a skill folder from package data into a ``ContentBlueprint(kind="playbook")``.

    ``package`` is the plugin's package (``__package__``); ``folder`` is the skill folder inside it
    (``"playbooks/acme-weekly-report"``) with ``SKILL.md`` at its top. The blueprint's ``slug`` is the SKILL.md ``name``,
    its ``description`` the frontmatter description, its ``name`` the ``title`` (default: the body's first ``# `` heading),
    and its ``payload`` is ``{"skill_md": text, "files": {relative path: text}}``: JSON-safe, nothing left to read later.
    Never required: a workspace chooses whether it wants it. ``scope="system"`` (0.16.0) stamps ``payload["scope"]``: list the
    blueprint in ``system_playbooks`` / ``playbooks`` and it is offered in every workspace instead (see the module notes).

    Raises ``ValueError`` for a folder that can't be shipped (missing SKILL.md, a script, a binary or oversize file, a path
    outside the folder). Run ``check_playbook`` for the rest.
    """
    if scope not in SCOPES:
        raise ValueError(f"playbook scope {scope!r} must be one of {', '.join(SCOPES)}")
    path = PurePosixPath(folder)
    if not folder or path.is_absolute() or ".." in path.parts or "\\" in folder:
        raise ValueError(f"playbook folder {folder!r} must be a relative path inside {package}")
    root = resources.files(package).joinpath(*path.parts)
    if not root.is_dir():
        raise ValueError(f"playbook folder {folder!r} not found in {package}")
    skill_md: str | None = None
    files: dict[str, str] = {}
    for rel, item in _walk(root):
        problem = _path_problem(rel)
        if problem:
            raise ValueError(f"{folder}: {problem}")
        try:
            text = item.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{folder}: '{rel}' is not UTF-8 text") from exc
        if rel == SKILL_FILE:
            skill_md = text
        else:
            files[rel] = text
    if skill_md is None:
        raise ValueError(f"{folder} has no {SKILL_FILE}")
    front, body = _front_matter(skill_md)
    name = front.get("name", "")
    blueprint = ContentBlueprint(
        kind="playbook",
        slug=name,
        name=title or _title(body, name),
        description=front.get("description", ""),
        required=False,
        payload={"skill_md": skill_md, "files": files, **({"scope": scope} if scope != "workspace" else {})},
    )
    problems = _payload_problems(blueprint.payload)
    if problems:
        raise ValueError(f"{folder}: {problems[0]}")
    return blueprint


def _shape_problem(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("skill_md"), str):
        return "payload must have a skill_md text"
    files = payload.get("files", {})
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        return "payload files must map a relative path to its text"
    if payload.get("scope", "workspace") not in SCOPES:
        return f"payload scope must be one of {', '.join(SCOPES)}"
    return None


def _payload_problems(payload: Any) -> list[str]:
    shape = _shape_problem(payload)
    if shape:
        return [shape]
    files = payload.get("files", {})
    problems: list[str] = []
    if len(payload["skill_md"].encode()) > SKILL_MAX_BYTES:
        problems.append(f"{SKILL_FILE} is over {SKILL_MAX_BYTES // 1024} KB")
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files; at most {MAX_FILES}")
    total = len(payload["skill_md"].encode())
    for rel, text in files.items():
        problem = _path_problem(rel)
        if problem:
            problems.append(problem)
        if rel == SKILL_FILE:
            problems.append(f"{SKILL_FILE} must not be in files")
        size = len(text.encode())
        total += size
        if size > FILE_MAX_BYTES:
            problems.append(f"'{rel}' is over {FILE_MAX_BYTES // 1024} KB")
    if total > TOTAL_MAX_BYTES:
        problems.append(f"the folder is over {TOTAL_MAX_BYTES // 1024} KB in all")
    return problems


def check_playbook(blueprint: ContentBlueprint, provider: Any = None, *, scope: str = "workspace") -> list[str]:
    """What is wrong with a Playbook blueprint: empty when Marvin can take it. With ``provider`` (an integration provider or a
    storage / AI plugin), the name must start with its slug and a hyphen. ``scope`` is where the blueprint is listed:
    ``"workspace"`` (``content``, the default) or ``"system"`` (``system_playbooks`` / ``playbooks``); a blueprint built for
    the other one is refused. System Playbooks are also linted (Rules 7 and 8; ``playbook_warnings`` has the soft ones)."""
    if blueprint.kind != "playbook":
        return [f"kind is '{blueprint.kind}', not 'playbook'"]
    if _shape_problem(blueprint.payload):
        return [_shape_problem(blueprint.payload)]
    problems = _payload_problems(blueprint.payload)
    built_for = blueprint.payload.get("scope", "workspace")
    if scope not in SCOPES:
        problems.append(f"scope {scope!r} must be one of {', '.join(SCOPES)}")
    elif built_for != scope:
        listed, where = ("content", "workspace") if scope == "workspace" else ("system_playbooks / playbooks", "system")
        problems.append(f'a {built_for} Playbook can\'t be listed in {listed}; build it with scope="{where}"')
    front, body = _front_matter(blueprint.payload["skill_md"])
    name, description = front.get("name", ""), front.get("description", "")
    if not NAME_PATTERN.match(name) or len(name) > NAME_MAX:
        problems.append(f"name '{name}' must be lowercase words joined by hyphens, at most {NAME_MAX} characters")
    if blueprint.slug != name:
        problems.append(f"slug '{blueprint.slug}' must equal the SKILL.md name '{name}'")
    prefix = provider_prefix(getattr(provider, "slug", "")) if provider is not None else ""
    if prefix and not name.startswith(prefix):
        problems.append(f"name '{name}' must start with the provider's slug: '{prefix}'")
    if not description.strip():
        problems.append("the frontmatter needs a description (agents choose the Playbook by it)")
    elif len(description) > DESCRIPTION_MAX:
        problems.append(f"description is {len(description)} characters; at most {DESCRIPTION_MAX}")
    if not body.strip():
        problems.append(f"{SKILL_FILE} has no body")
    files = blueprint.payload.get("files", {})
    for ref in sorted(set(_REFERENCED.findall(body))):
        if ref not in files:
            problems.append(f"{SKILL_FILE} refers to '{ref}', which is not in the folder")
    tools = re.split(r"[\s,]+", front.get("allowed-tools", front.get("allowed_tools", "")).strip())
    for tool in tools:
        if tool and not _TOOL_NAME.match(tool):
            problems.append(f"allowed-tools '{tool}' doesn't look like a Marvin tool name (e.g. list_events)")
    if built_for == "system":
        for tool in tools:
            if tool in _AI_WRITERS:
                problems.append(f"allowed-tools '{tool}': a system Playbook writes drafts itself (create_draft / update_draft)")
        texts = {SKILL_FILE: blueprint.payload["skill_md"], **files}
        for rel, text in texts.items():
            for token in sorted(set(_UUID.findall(text))):
                problems.append(f"{rel} has the id '{token}'; name things by slug, ids differ in every workspace")
    return problems


def playbook_warnings(blueprint: ContentBlueprint) -> list[str]:
    """Soft problems with a Playbook (Marvin shows them, nothing is refused): ``allowed-tools`` set at all, and
    ``compose_entry`` / ``revise_entry`` named in the body without saying not to use them."""
    payload = blueprint.payload
    if _shape_problem(payload):
        return []
    front, body = _front_matter(payload["skill_md"])
    warnings: list[str] = []
    if re.split(r"[\s,]+", front.get("allowed-tools", front.get("allowed_tools", "")).strip())[0]:
        warnings.append("allowed-tools is set; it limits the agent's tools while this Playbook is loaded, so rarely needed")
    for line in body.split("\n"):
        if any(w in line for w in _AI_WRITERS) and not _NEGATION.search(line):
            warnings.append(
                f"the body points at compose_entry / revise_entry ('{line.strip()[:60]}'); write the text yourself with create_draft / update_draft"
            )
    return warnings


__all__ = [
    "FILE_MAX_BYTES",
    "MAX_FILES",
    "SCOPES",
    "SKILL_MAX_BYTES",
    "TOTAL_MAX_BYTES",
    "check_playbook",
    "provider_prefix",
    "playbook_blueprint",
    "playbook_warnings",
]
