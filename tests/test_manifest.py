"""The manifest is a contract: what it declares is what register() does."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_project, package_json

import atlas
from atlas import schemas
from atlas.settings import SCHEMA_KEYS

PLUGIN_YAML = Path(__file__).resolve().parents[1] / "plugin.yaml"


@pytest.fixture(scope="module")
def manifest():
    try:
        import hermes_yaml as yaml
    except Exception:  # pragma: no cover - plain pytest without the Hermes source
        yaml = pytest.importorskip("yaml")
    return yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))


def test_manifest_identity_and_floor(manifest):
    assert manifest["name"] == "atlas"
    assert manifest["manifest_version"] == 2
    assert manifest["api_version"] == 1
    assert manifest["license"] == "MIT"
    assert manifest["author"] == "kyssta"
    assert manifest["homepage"] == "https://github.com/okyssta/hermes-atlas"
    assert manifest["requires_hermes"] == ">=0.22.0"
    assert isinstance(manifest["version"], str) and manifest["version"].count(".") == 2


def test_description_ends_with_a_disclosure(manifest):
    description = manifest["description"]
    assert "Disclosure" in description
    tail = description.rsplit("Disclosure", 1)[1]
    assert tail.lstrip().startswith("—")
    assert len(tail) > 200, "the disclosure has to say something specific"
    assert description.rstrip().endswith(".")


def test_every_config_key_is_declared_in_both_places(manifest):
    assert set(manifest["config_schema"]) == set(SCHEMA_KEYS)


def test_declared_tools_match_the_schemas_we_register(manifest):
    assert set(manifest["provides_tools"]) == {schema["name"] for schema in schemas.ALL}
    assert [schema["name"] for schema in schemas.ALL] == list(manifest["provides_tools"])


def test_declared_hooks_match_reality(manifest, register_ctx):
    ctx = register_ctx()
    assert manifest["provides_hooks"] == []
    assert ctx.hooks == []


def test_register_registers_every_tool_with_a_handler(manifest, register_ctx):
    ctx = register_ctx()
    assert set(ctx.tools) == set(manifest["provides_tools"])
    for name, entry in ctx.tools.items():
        assert entry["toolset"] == "atlas"
        assert entry["schema"]["name"] == name
        assert callable(entry["handler"])


def test_register_registers_the_slash_command(manifest, register_ctx):
    ctx = register_ctx()
    assert set(ctx.commands) == {"atlas"}
    assert callable(ctx.commands["atlas"])


def test_no_python_dependencies_are_declared(manifest):
    assert not manifest.get("python_dependencies")
    assert not manifest.get("pip_dependencies")


def test_handlers_are_reachable_through_the_registration(register_ctx, tmp_path, monkeypatch):
    project = make_project(
        tmp_path / "code",
        "demo",
        {"package.json": package_json(scripts={"test": "jest"})},
    )
    monkeypatch.chdir(project)
    atlas.reset_caches()
    ctx = register_ctx()
    text = ctx.tools["project_info"]["handler"]({})
    assert "demo" in text
    assert "npm run test" in text


def test_slash_command_help_lists_the_surfaces(register_ctx):
    ctx = register_ctx()
    text = ctx.commands["atlas"]("help")
    assert "/atlas list" in text
    assert "/atlas info" in text
    assert "/atlas refresh" in text
