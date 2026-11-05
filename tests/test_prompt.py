"""The system-prompt section: fires inside a project, stays silent outside, stays bounded."""

from __future__ import annotations

from types import MappingProxyType

from conftest import make_project, package_json

import atlas


def _uv_project(tmp_path):
    return make_project(
        tmp_path / "code",
        "demo",
        {
            "pyproject.toml": '[project]\nname = "demo"\ndependencies = ["pytest>=8"]\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "uv.lock": "",
            "tests/test_x.py": "",
        },
    )


def test_section_is_registered_with_the_declared_id_position_and_budget(register_ctx):
    ctx = register_ctx()
    assert set(ctx.sections) == {"atlas.project"}
    content, position, max_chars = ctx.sections["atlas.project"]
    assert callable(content)
    assert position == "after_memory"
    assert max_chars == 900


def test_card_renders_inside_a_project_and_is_bounded(tmp_path, register_ctx):
    project = _uv_project(tmp_path)
    content = register_ctx().sections["atlas.project"][0]
    text = content(MappingProxyType({"cwd": str(project), "session_id": "s1"}))
    assert "demo" in text
    assert "python (uv)" in text
    assert "test: uv run pytest" in text
    assert "call project_info for the full card" in text.lower()
    assert len(text) <= 900


def test_card_renders_from_a_subdirectory(tmp_path, register_ctx):
    project = _uv_project(tmp_path)
    deep = project / "src" / "deep"
    deep.mkdir(parents=True)
    content = register_ctx().sections["atlas.project"][0]
    text = content({"cwd": str(deep)})
    assert "demo" in text
    assert str(project.resolve()) in text


def test_card_is_empty_outside_any_project(tmp_path, register_ctx):
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    (elsewhere / "notes.txt").write_text("nothing to see")
    content = register_ctx().sections["atlas.project"][0]
    assert content({"cwd": str(elsewhere)}) == ""
    assert content({}) in {"", content({"cwd": str(elsewhere)})}


def test_card_can_be_disabled_by_config(tmp_path, register_ctx):
    project = _uv_project(tmp_path)
    content = register_ctx(inject_prompt_section=False).sections["atlas.project"][0]
    assert content({"cwd": str(project)}) == ""


def test_card_is_dropped_when_the_plugin_is_disabled(tmp_path, register_ctx):
    project = _uv_project(tmp_path)
    content = register_ctx(enabled=False).sections["atlas.project"][0]
    assert content({"cwd": str(project)}) == ""


def test_card_survives_a_broken_index(tmp_path, register_ctx, monkeypatch):
    project = _uv_project(tmp_path)
    content = register_ctx().sections["atlas.project"][0]

    def boom(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(atlas.store, "resolve_cwd", boom)
    assert content({"cwd": str(project)}) == ""


def test_card_is_cached_per_project(tmp_path, register_ctx, monkeypatch):
    project = _uv_project(tmp_path)
    content = register_ctx().sections["atlas.project"][0]
    first = content({"cwd": str(project)})
    calls = {"n": 0}
    original = atlas.tools.prompt_card

    def counting(card, limit=900):
        calls["n"] += 1
        return original(card, limit=limit)

    monkeypatch.setattr(atlas.tools, "prompt_card", counting)
    second = content({"cwd": str(project)})
    assert first == second
    assert calls["n"] == 0  # served from the section cache, not re-rendered


def test_card_includes_env_names_but_never_values(tmp_path, register_ctx):
    project = make_project(
        tmp_path / "code",
        "demo",
        {
            "package.json": package_json(scripts={"test": "jest", "dev": "vite"}),
            ".env": "STRIPE_KEY=sk-test-abcdef\n",
        },
    )
    content = register_ctx().sections["atlas.project"][0]
    text = content({"cwd": str(project)})
    assert ".env" in text
    assert "sk-test-abcdef" not in text
