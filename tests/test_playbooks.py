"""Playbooks an integration ships: a skill folder in package data, read into a blueprint and checked."""

import json
import sys
import textwrap

import pytest

from marvin_integration_sdk import ContentBlueprint, IntegrationProvider, check_playbook, playbook_blueprint, playbook_warnings
from marvin_integration_sdk.ai import AIProvider, AIProviderPlugin
from marvin_integration_sdk.storage import StoragePlugin
from marvin_integration_sdk.testing import PluginPlaybooksContract
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


# ── system Playbooks (0.16.0) ──────────────────────────────────────────────────────────────────────────────────────────


def _system(skill=SKILL, **kw):
    return ContentBlueprint(
        kind="playbook",
        slug="acme-weekly-report",
        name="N",
        payload={"skill_md": skill, "files": {"references/checklist.md": "x"}, "scope": "system", **kw},
    )


def test_scope_system_is_stamped_and_workspace_leaves_the_payload_alone(package):
    pkg, _ = package
    assert playbook_blueprint(pkg, "playbooks/acme-weekly-report", scope="system").payload["scope"] == "system"
    assert "scope" not in playbook_blueprint(pkg, "playbooks/acme-weekly-report").payload
    with pytest.raises(ValueError, match="scope"):
        playbook_blueprint(pkg, "playbooks/acme-weekly-report", scope="global")


def test_a_blueprint_must_be_listed_where_it_was_built_for(package):
    pkg, _ = package
    system = playbook_blueprint(pkg, "playbooks/acme-weekly-report", scope="system")
    workspace = playbook_blueprint(pkg, "playbooks/acme-weekly-report")
    assert check_playbook(system, _provider(), scope="system") == []
    assert check_playbook(workspace, _provider()) == []
    assert any("can't be listed in content" in p for p in check_playbook(system, _provider()))
    assert any("can't be listed in system_playbooks" in p for p in check_playbook(workspace, _provider(), scope="system"))
    assert any("scope 'x'" in p for p in check_playbook(workspace, _provider(), scope="x"))
    assert any("payload scope" in p for p in check_playbook(_bp(scope="nope"), _provider()))


def test_the_lint_refuses_ids_and_ai_writers_in_allowed_tools_for_system_playbooks_only():
    with_id = SKILL + "\nThe image 3f2b8c1e-9d4a-4e6b-8a1f-0c2d3e4f5a6b is the logo.\n"
    assert any("has the id '3f2b8c1e" in p for p in check_playbook(_system(with_id), _provider(), scope="system"))
    assert check_playbook(_bp(skill_md=with_id), _provider()) == []
    drafting = SKILL.replace("list_events get_entry", "get_entry compose_entry")
    assert any("allowed-tools 'compose_entry'" in p for p in check_playbook(_system(drafting), _provider(), scope="system"))
    assert not any("compose_entry" in p for p in check_playbook(_bp(skill_md=drafting), _provider()))
    in_file = _system()
    in_file.payload["files"]["references/checklist.md"] = "see 3f2b8c1e-9d4a-4e6b-8a1f-0c2d3e4f5a6b"
    assert any("references/checklist.md has the id" in p for p in check_playbook(in_file, _provider(), scope="system"))


def test_warnings_cover_allowed_tools_and_ai_writers_in_the_body():
    assert any("allowed-tools is set" in w for w in playbook_warnings(_bp()))
    quiet = SKILL.replace("allowed-tools: list_events get_entry\n", "")
    assert playbook_warnings(_bp(skill_md=quiet)) == []
    pointing = quiet + "\nSave it with compose_entry.\n"
    assert any("compose_entry" in w for w in playbook_warnings(_bp(skill_md=pointing)))
    refusing = quiet + "\nNever use revise_entry here; write the text yourself.\n"
    assert playbook_warnings(_bp(skill_md=refusing)) == []
    assert playbook_warnings(ContentBlueprint(kind="playbook", slug="a", name="A", payload={})) == []


def test_providers_plugins_default_to_no_system_playbooks_and_info_lists_them(package):
    pkg, _ = package
    assert _provider().system_playbooks == ()
    assert _provider().info()["system_playbooks"] == []

    class Acme(IntegrationProvider):
        slug = "acme"
        name = "Acme"
        system_playbooks = (playbook_blueprint(pkg, "playbooks/acme-weekly-report", scope="system"),)

    assert [b["slug"] for b in Acme().info()["system_playbooks"]] == ["acme-weekly-report"]
    json.dumps(Acme().info())


def test_storage_and_ai_plugins_gain_a_playbooks_field_that_defaults_empty():
    from marvin_integration_sdk.ai.fake import FakeAIProvider
    from marvin_integration_sdk.storage.memory import MemoryStorageProvider

    assert StoragePlugin(slug="mem", name="Mem", provider=MemoryStorageProvider).playbooks == ()
    assert AIProviderPlugin(slug=FakeAIProvider.provider_type, name="Fake", provider=FakeAIProvider).playbooks == ()
    assert issubclass(FakeAIProvider, AIProvider)


def test_the_contract_checks_every_list(package):
    pkg, _ = package
    system = playbook_blueprint(pkg, "playbooks/acme-weekly-report", scope="system")
    from marvin_integration_sdk.storage.memory import MemoryStorageProvider

    class Acme(IntegrationProvider):
        slug = "acme"
        name = "Acme"
        system_playbooks = (system,)

    class Contract(IntegrationContract):
        pass

    Contract().test_system_playbooks_are_well_formed(Acme())
    Contract().test_playbooks_are_well_formed(Acme())

    class Both(Acme):
        content = (playbook_blueprint(pkg, "playbooks/acme-weekly-report"),)

    with pytest.raises(AssertionError, match="both content and system_playbooks"):
        Contract().test_system_playbooks_are_well_formed(Both())

    class Misplaced(Acme):
        system_playbooks = (playbook_blueprint(pkg, "playbooks/acme-weekly-report"),)

    with pytest.raises(AssertionError, match="listed in system_playbooks"):
        Contract().test_system_playbooks_are_well_formed(Misplaced())

    class Misnamed(Acme):
        slug = "other"

    with pytest.raises(AssertionError, match="must start with the provider's slug"):
        Contract().test_system_playbooks_are_well_formed(Misnamed())

    plugin = StoragePlugin(slug="acme", name="Acme storage", provider=MemoryStorageProvider, playbooks=(system,))
    PluginPlaybooksContract().test_system_playbooks_are_well_formed(plugin)
    bad = StoragePlugin(
        slug="acme", name="Acme storage", provider=MemoryStorageProvider, playbooks=(playbook_blueprint(pkg, "playbooks/acme-weekly-report"),)
    )
    with pytest.raises(AssertionError, match="listed in system_playbooks"):
        PluginPlaybooksContract().test_system_playbooks_are_well_formed(bad)
    twice = StoragePlugin(slug="acme", name="Acme storage", provider=MemoryStorageProvider, playbooks=(system, system))
    with pytest.raises(AssertionError, match="duplicate"):
        PluginPlaybooksContract().test_system_playbooks_are_well_formed(twice)
