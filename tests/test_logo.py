"""A provider's logo is read from its own package data; anything odd degrades to None, never raises."""

import importlib
import sys
import textwrap

import pytest

from marvin_integration_sdk import IntegrationProvider, load_logo

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def make_package(tmp_path, monkeypatch):
    """Build an importable package `<name>` holding a provider module plus the given data files."""
    monkeypatch.syspath_prepend(str(tmp_path))
    made = []

    def make(name: str, logo: str, files: dict[str, bytes], in_init: bool = False):
        pkg = tmp_path / name
        pkg.mkdir()
        source = textwrap.dedent(
            f"""
            from marvin_integration_sdk import IntegrationProvider

            class Provider(IntegrationProvider):
                slug = "{name}"
                name = "{name}"
                logo = {logo!r}
            """
        )
        if in_init:
            (pkg / "__init__.py").write_text(source)
        else:
            (pkg / "__init__.py").write_text("")
            (pkg / "provider.py").write_text(source)
        for rel, data in files.items():
            target = pkg / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        made.append(name)
        module = importlib.import_module(name if in_init else f"{name}.provider")
        return module.Provider

    yield make
    for name in made:
        for mod in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
            del sys.modules[mod]


def test_svg_logo_is_read_with_its_content_type(make_package):
    provider = make_package("logo_pkg_svg", "logo.svg", {"logo.svg": SVG})
    assert load_logo(provider) == (SVG, "image/svg+xml")
    assert load_logo(provider()) == (SVG, "image/svg+xml")  # class or instance
    assert provider().info()["has_logo"] is True


def test_png_logo_in_a_subfolder(make_package):
    provider = make_package("logo_pkg_png", "assets/Logo.PNG", {"assets/Logo.PNG": PNG})
    assert load_logo(provider) == (PNG, "image/png")


def test_provider_defined_in_the_package_init(make_package):
    provider = make_package("logo_pkg_init", "logo.svg", {"logo.svg": SVG}, in_init=True)
    assert load_logo(provider) == (SVG, "image/svg+xml")


def test_no_logo_declared_is_none():
    class _Plain(IntegrationProvider):
        slug = "plain"
        name = "Plain"

    assert load_logo(_Plain) is None
    assert _Plain().info()["has_logo"] is False
    assert _Plain().info()["icon"] == ""


def test_missing_file_is_none(make_package):
    provider = make_package("logo_pkg_missing", "logo.svg", {})
    assert load_logo(provider) is None
    assert provider().info()["has_logo"] is False


@pytest.mark.parametrize("logo", ["logo.gif", "logo", "../logo.svg", "/etc/logo.svg", "sub\\logo.svg", "a/../../logo.svg"])
def test_unsupported_or_escaping_paths_are_none(make_package, logo):
    provider = make_package(f"logo_pkg_bad_{abs(hash(logo))}", logo, {"logo.gif": b"GIF89a", "logo": SVG})
    assert load_logo(provider) is None


def test_a_non_string_logo_is_none():
    class _Odd(IntegrationProvider):
        slug = "odd"
        name = "Odd"
        logo = 42

    assert load_logo(_Odd) is None
