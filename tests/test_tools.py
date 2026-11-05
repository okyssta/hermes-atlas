"""Handlers: the text a model actually sees, and the never-raise contract."""

from __future__ import annotations

import pytest
from conftest import gitify, make_project, package_json

import atlas
from atlas import store, tools

PYPROJECT_UV = """
[project]
name = "demo"
dependencies = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


@pytest.fixture()
def uv_project(tmp_path, hermes_home, monkeypatch, bind_config):
    code = tmp_path / "code"
    project = make_project(
        code,
        "demo",
        {"pyproject.toml": PYPROJECT_UV, "uv.lock": "", "tests/test_x.py": "", "README.md": "# Demo\n"},
    )
    gitify(project, branch="main", timestamp=1762300000)
    monkeypatch.chdir(project)
    bind_config(roots=[str(code)])
    return project


def test_project_list_shows_name_stack_path_and_hint(uv_project):
    text = tools.project_list({"query": ""})
    assert "Atlas — 1 project" in text
    assert "demo" in text
    assert "python (uv)" in text
    assert str(uv_project.resolve()) in text
    assert "project_info" in text


def test_project_list_query_filters_and_empty_result_explains_itself(uv_project):
    assert "demo" in tools.project_list({"query": "python"})
    empty = tools.project_list({"query": "rust"})
    assert "no projects" in empty.lower()
    assert "project_refresh" in empty


def test_project_info_defaults_to_the_cwd_and_shows_commands_with_reasons(uv_project):
    text = tools.project_info({})
    assert "demo — python (uv)" in text
    assert "uv sync" in text
    assert "# uv.lock present" in text
    assert "uv run pytest" in text
    assert "branch" in text
    assert "env files" not in text  # none exist, so no empty section


def test_project_info_accepts_a_name(uv_project):
    assert "demo — python (uv)" in tools.project_info({"path_or_name": "demo"})


def test_project_commands_is_copy_pasteable(uv_project):
    text = tools.project_commands({"path_or_name": "demo"})
    lines = text.splitlines()
    assert lines[0].startswith("# demo — ")
    assert "uv sync" in lines
    assert "uv run pytest" in lines
    assert all("# " not in line for line in lines[1:])


def test_project_refresh_reports_the_scan(uv_project):
    text = tools.project_refresh({})
    assert "Atlas rescan" in text
    assert "1 projects indexed" in text
    assert "visited" in text


def test_unknown_project_says_what_to_do_next(tmp_path, hermes_home, monkeypatch, bind_config):
    monkeypatch.chdir(tmp_path)
    bind_config(roots=[str(tmp_path)])
    text = tools.project_info({"path_or_name": "definitely-not-a-project"})
    assert "No project" in text
    assert "project_refresh" in text
    assert "known projects" not in text


def test_handlers_never_raise_on_bad_arguments(uv_project):
    for handler in (tools.project_list, tools.project_info, tools.project_commands, tools.project_refresh):
        text = handler(None)
        assert isinstance(text, str)
        text = handler({"path_or_name": 12345, "query": ["not", "a", "string"]})
        assert isinstance(text, str)


def test_handler_failure_is_reported_not_raised(uv_project, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("index is on fire")

    monkeypatch.setattr(store, "all_projects", boom)
    text = tools.project_list({})
    assert text.startswith("Error: atlas could not list projects")
    assert "index is on fire" in text


def test_prompt_card_dedupes_tasks_and_fits_the_cap(tmp_path, hermes_home, monkeypatch, bind_config):
    project = make_project(
        tmp_path / "code",
        "multi",
        {
            "package.json": package_json(scripts={"test": "vitest run", "build": "vite build"}),
            "pnpm-lock.yaml": "",
            "docker-compose.yml": "services:\n  db:\n    image: postgres\n",
        },
    )
    monkeypatch.chdir(project)
    bind_config(roots=[str(tmp_path / "code")])
    card = store.resolve_cwd(atlas.current_settings(), project)
    text = tools.prompt_card(card)
    assert len(text) <= 900
    assert text.count("test: ") == 1
    assert "project_info for the full card" in text


def test_prompt_card_truncates_a_huge_project():
    commands = [
        {
            "task": task,
            "command": f"uv run {task} " + " ".join(f"--flag{i}=value{i}" for i in range(8)),
            "reason": "synthetic",
        }
        for task in ("install", "test", "lint", "typecheck", "build", "format", "run")
    ]
    project = {"name": "huge", "path": "/tmp/huge", "stack": "python", "commands": commands}
    text = tools.prompt_card(project, limit=900)
    assert len(text) <= 900
    assert text.endswith("Call project_info for the full card.")
    assert text.count("\n") >= 3  # header, path and at least one command survived


def test_project_info_refreshes_a_stale_row(tmp_path, hermes_home, monkeypatch, bind_config):
    code = tmp_path / "code"
    project = make_project(code, "app", {"pyproject.toml": '[project]\nname = "app"\nversion = "0.1.0"\n'})
    bind_config(roots=[str(code)], cache_ttl_minutes=0)
    monkeypatch.chdir(project)
    assert "pip install -e ." in tools.project_info({})
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    refreshed = tools.project_info({"path_or_name": str(project)})
    assert "uv sync" in refreshed
    assert "pip install -e ." not in refreshed


def test_slash_command_covers_list_info_commands_show_and_forget(uv_project):
    from atlas import commands

    assert "demo" in commands.handle("list")
    assert "demo — python (uv)" in commands.handle("info demo")
    assert "uv run pytest" in commands.handle("commands demo")
    assert "Atlas: project demo" in commands.handle("show")
    assert "projects indexed" in commands.handle("status")
    assert "/atlas refresh" in commands.handle("help")
    assert "cleared" in commands.handle("forget")
    assert store.count() == 0


def test_handlers_explain_when_the_plugin_is_disabled(tmp_path, hermes_home, bind_config):
    bind_config(enabled=False, roots=[str(tmp_path)])
    assert "disabled" in tools.project_list({})
    assert "disabled" in tools.project_info({})
    assert "disabled" in tools.project_commands({})
    assert "disabled" in tools.project_refresh({})


def test_slash_command_reports_a_broken_index_instead_of_raising(tmp_path, hermes_home, bind_config, monkeypatch):
    from atlas import commands

    bind_config(roots=[str(tmp_path)])

    def boom(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(store, "stats", boom)
    text = commands.handle("status")
    assert text.startswith("atlas: the command failed")
    assert "db gone" in text
