"""Playbooks an integration ships: a skill folder in package data, read into a blueprint and checked."""

import json
import sys
import textwrap

import pytest

from marvin_integration_sdk import ContentBlueprint, IntegrationProvider, check_playbook, playbook_blueprint
from marvin_integration_sdk.playbooks import provider_prefix
from marvin_integration_sdk.testing import IntegrationContract

SKILL = """\
---
name: acme-weekly-report
description: How to write Acme's weekly report. Use it when drafting one.
allowed-tools: list_events get_entry
---

# Acme weekly report

Follow `references/checklist.md` before you save.
"""


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A throwaway importable package with one skill folder; returns (package name, folder path)."""
    root = tmp_path / "acme_plugin"
    folder = root / "playbooks" / "acme-weekly-report"
    (folder / "references").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (folder / "SKILL.md").write_text(SKILL)
    (folder / "references" / "checklist.md").write_text("- one\n- two\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "acme_plugin", folder
    for name in [m for m in sys.modules if m.startswith("acme_plugin")]:
        del sys.modules[name]


def _provider(*content):
    class Acme(IntegrationProvider):
        slug = "acme"
        name = "Acme"

    Acme.content = tuple(content)
    return Acme()


def test_the_helper_reads_a_folder_into_a_blueprint(package):
    pkg, _ = package
    bp = playbook_blueprint(pkg, "playbooks/acme-weekly-report")
    assert (bp.kind, bp.slug, bp.name, bp.required) == ("playbook", "acme-weekly-report", "Acme weekly report", False)
    assert bp.description.startswith("How to write Acme's weekly report")
    assert bp.payload["skill_md"] == SKILL
    assert bp.payload["files"] == {"references/checklist.md": "- one\n- two\n"}
    json.dumps(bp.payload)
    assert check_playbook(bp, _provider()) == []


def test_a_title_can_be_given(package):
    assert playbook_blueprint(package[0], "playbooks/acme-weekly-report", title="Weekly report").name == "Weekly report"


@pytest.mark.parametrize(
    "rel, text, message",
    [
        ("scripts/run.sh", "echo", "script"),
        ("references/tool.py", "x = 1", "script"),
        ("references/logo.png", "x", "must be one of"),
        ("references/big.md", "x" * (64 * 1024 + 1), "over 64 KB"),
    ],
)
def test_the_helper_refuses_what_cant_ship(package, rel, text, message):
    pkg, folder = package
    target = folder / rel
    target.parent.mkdir(exist_ok=True)
    target.write_text(text)
    with pytest.raises(ValueError, match=message):
        playbook_blueprint(pkg, "playbooks/acme-weekly-report")


def test_the_helper_refuses_a_missing_folder_a_missing_skill_and_a_path_outside(package):
    pkg, folder = package
    with pytest.raises(ValueError, match="not found"):
        playbook_blueprint(pkg, "playbooks/nope")
    with pytest.raises(ValueError, match="relative"):
        playbook_blueprint(pkg, "../playbooks")
    (folder / "SKILL.md").unlink()
    with pytest.raises(ValueError, match="no SKILL.md"):
        playbook_blueprint(pkg, "playbooks/acme-weekly-report")


def test_too_many_or_too_large_a_folder_is_refused(package):
    pkg, folder = package
    for i in range(21):
        (folder / "references" / f"f{i}.md").write_text("x")
    with pytest.raises(ValueError, match="at most 20"):
        playbook_blueprint(pkg, "playbooks/acme-weekly-report")


def _bp(**payload):
    base = {"skill_md": SKILL, "files": {"references/checklist.md": "x"}}
    return ContentBlueprint(kind="playbook", slug=payload.pop("slug", "acme-weekly-report"), name="N", payload={**base, **payload})


@pytest.mark.parametrize(
    "blueprint, problem",
    [
        (_bp(slug="other-name"), "must equal the SKILL.md name"),
        (_bp(skill_md=SKILL.replace("name: acme-weekly-report", "name: Weekly Report")), "lowercase words"),
        (_bp(skill_md=SKILL.replace("acme-weekly-report", "weekly-report"), slug="weekly-report"), "must start with the provider's slug"),
        (_bp(skill_md=SKILL.replace("description:", "summary:")), "needs a description"),
        (_bp(skill_md=SKILL.replace("Acme's weekly report. Use it when drafting one.", "x" * 1025)), "at most 1024"),
        (_bp(files={}), "refers to 'references/checklist.md'"),
        (_bp(skill_md=SKILL.replace("list_events", "Bash(git add:*)")), "doesn't look like a Marvin tool"),
        (_bp(files={"../escape.md": "x", "references/checklist.md": "x"}), "without '..'"),
        (_bp(files={"scripts/a.sh": "x", "references/checklist.md": "x"}), "script"),
        (_bp(skill_md="no frontmatter"), "needs a description"),
        (ContentBlueprint(kind="playbook", slug="a", name="A", payload={}), "skill_md"),
        (ContentBlueprint(kind="workflow", slug="a", name="A"), "not 'playbook'"),
    ],
)
def test_check_playbook_names_the_problem(blueprint, problem):
    assert any(problem in p for p in check_playbook(blueprint, _provider())), check_playbook(blueprint, _provider())


def test_an_underscore_in_the_providers_slug_is_a_hyphen_in_the_prefix():
    class Pages(IntegrationProvider):
        slug = "acme_pages"
        name = "Acme Pages"

    assert provider_prefix("acme_pages") == "acme-pages-" and provider_prefix("") == ""
    bp = _bp(skill_md=SKILL.replace("acme-weekly-report", "acme-pages-report"), slug="acme-pages-report")
    assert check_playbook(bp, Pages()) == []
    assert any("must start with" in p for p in check_playbook(_bp(), Pages()))


def test_the_provider_prefix_is_only_checked_with_a_provider():
    bp = _bp(skill_md=SKILL.replace("acme-weekly-report", "weekly-report"), slug="weekly-report")
    assert check_playbook(bp) == []


def test_yaml_folded_descriptions_and_quotes_are_read():
    skill = textwrap.dedent(
        """\
        ---
        name: acme-x
        description: "Quoted: with a colon"
        ---
        # X
        body
        """
    )
    bp = ContentBlueprint(kind="playbook", slug="acme-x", name="X", payload={"skill_md": skill, "files": {}})
    assert check_playbook(bp, _provider()) == []


def test_the_conformance_kit_checks_playbooks(package):
    pkg, _ = package
    good = _provider(playbook_blueprint(pkg, "playbooks/acme-weekly-report"))

    class Good(IntegrationContract):
        @pytest.fixture
        def provider(self):
            return good

    Good().test_playbooks_are_well_formed(good)
    bad = _provider(_bp(slug="other-name"))
    with pytest.raises(AssertionError, match="other-name"):
        Good().test_playbooks_are_well_formed(bad)
