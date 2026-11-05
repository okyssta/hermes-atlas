"""Detection: marker files in, exact commands out — with the reason attached."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import gitify, make_project, package_json

from atlas import detect

PYPROJECT_UV = """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "demo"
version = "0.1.0"
dependencies = ["httpx>=0.27", "pytest>=8"]

[project.scripts]
demo = "demo.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F"]
"""

PYPROJECT_POETRY = """
[tool.poetry]
name = "demo"
version = "0.1.0"

[tool.poetry.dependencies]
python = "^3.11"
pytest = "^8.0"
"""


def _tasks(card):
    return {command["task"]: command for command in card["commands"]}


def test_python_uv_project_gets_uv_commands(tmp_path):
    project = make_project(
        tmp_path,
        "demo",
        {
            "pyproject.toml": PYPROJECT_UV,
            "uv.lock": "version = 1\n",
            "conftest.py": "",
        },
    )
    card = detect.detect_dir(project)
    assert card is not None
    assert card["stack"] == "python"
    assert card["package_manager"] == "uv"
    tasks = _tasks(card)
    assert tasks["install"]["command"] == "uv sync"
    assert tasks["install"]["reason"] == "uv.lock present"
    assert tasks["test"]["command"] == "uv run pytest"
    assert "pyproject.toml" in tasks["test"]["reason"]
    assert tasks["lint"]["command"] == "uv run ruff check ."
    assert tasks["build"]["command"] == "uv build"
    assert tasks["run"]["command"] == "demo"


def test_python_poetry_project_uses_poetry_run(tmp_path):
    project = make_project(
        tmp_path, "poem", {"pyproject.toml": PYPROJECT_POETRY, "poetry.lock": ""}
    )
    card = detect.detect_dir(project)
    assert card["package_manager"] == "poetry"
    tasks = _tasks(card)
    assert tasks["install"]["command"] == "poetry install"
    assert tasks["test"]["command"] == "poetry run pytest"
    assert "dependencies" in tasks["test"]["reason"]


def test_python_project_without_pytest_declares_no_test_command(tmp_path):
    project = make_project(
        tmp_path,
        "lib",
        {"pyproject.toml": "[project]\nname = \"lib\"\nversion = \"0.1.0\"\n"},
    )
    card = detect.detect_dir(project)
    assert "test" not in _tasks(card)
    assert any("no test command" in note for note in card["notes"])


def test_node_pnpm_scripts_become_commands_with_reasons(tmp_path):
    project = make_project(
        tmp_path,
        "web",
        {
            "package.json": package_json(
                scripts={
                    "test": "vitest run",
                    "lint": "eslint .",
                    "build": "vite build",
                    "dev": "vite --port 5173",
                }
            ),
            "pnpm-lock.yaml": "",
        },
    )
    card = detect.detect_dir(project)
    assert card["stack"] == "node"
    assert card["package_manager"] == "pnpm"
    assert card["stack_label"] == "node (pnpm)"
    tasks = _tasks(card)
    assert tasks["install"]["command"] == "pnpm install"
    assert tasks["install"]["reason"] == "pnpm-lock.yaml"
    assert tasks["test"]["command"] == "pnpm run test"
    assert tasks["test"]["reason"] == "package.json scripts.test"
    assert tasks["run"]["command"] == "pnpm run dev"
    assert {"port": 5173, "source": "package.json scripts"} in card["ports"]


def test_node_npm_defaults_and_start_script(tmp_path):
    project = make_project(
        tmp_path,
        "api",
        {
            "package.json": package_json(scripts={"start": "node server.js --port 8080"}),
            "package-lock.json": "{}",
        },
    )
    card = detect.detect_dir(project)
    assert card["package_manager"] == "npm"
    tasks = _tasks(card)
    assert tasks["install"]["command"] == "npm install"
    # No scripts.test -> no invented "npm test".
    assert "test" not in tasks
    assert tasks["run"]["command"] == "npm run start"
    assert card["ports"][0]["port"] == 8080


def test_rust_project_uses_cargo(tmp_path):
    project = make_project(
        tmp_path,
        "oxide",
        {"Cargo.toml": "[package]\nname = \"oxide\"\nversion = \"0.1.0\"\nedition = \"2021\"\n", "src/main.rs": "fn main() {}\n"},
    )
    card = detect.detect_dir(project)
    assert card["stack"] == "rust"
    tasks = _tasks(card)
    assert tasks["test"]["command"] == "cargo test"
    assert tasks["build"]["command"] == "cargo build"
    assert tasks["run"]["command"] == "cargo run"
    assert tasks["lint"]["command"].startswith("cargo clippy")


def test_go_module_uses_go_toolchain_and_cmd_binary(tmp_path):
    project = make_project(
        tmp_path,
        "svc",
        {
            "go.mod": "module example.com/svc\n\ngo 1.22\n",
            "cmd/server/main.go": "package main\n\nfunc main() {}\n",
        },
    )
    card = detect.detect_dir(project)
    assert card["stack"] == "go"
    tasks = _tasks(card)
    assert tasks["test"]["command"] == "go test ./..."
    assert tasks["lint"]["command"] == "go vet ./..."
    assert tasks["run"]["command"] == "go run ./cmd/server"


def test_makefile_targets_are_parsed_not_executed(tmp_path):
    makefile = (
        "VAR = value\n"
        ".PHONY: test lint fmt\n"
        "\n"
        "test:\n"
        "\t@echo test\n"
        "\n"
        "lint:\n"
        "\t@echo lint\n"
        "\n"
        "fmt:\n"
        "\t@echo fmt\n"
        "\n"
        "%.o: %.c\n"
        "\t$(CC) -c $<\n"
        "\n"
        "danger: ; rm -rf $(HOME)\n"
    )
    project = make_project(tmp_path, "maker", {"Makefile": makefile})
    card = detect.detect_dir(project)
    assert card["stack"] == "make"
    tasks = _tasks(card)
    assert tasks["test"]["command"] == "make test"
    assert tasks["test"]["reason"] == "Makefile target 'test'"
    assert tasks["lint"]["command"] == "make lint"
    assert tasks["format"]["command"] == "make fmt"
    targets = [command["command"] for command in card["commands"]]
    assert "make VAR" not in targets
    assert "make .PHONY" not in targets
    assert not any("%" in command for command in targets)
    # "danger" is a real target but none of the task names, so it is listed as a
    # note instead of being turned into a command.
    assert not any("danger" in command for command in targets)
    assert any("danger" in note for note in card["notes"])


def test_ignored_directories_are_never_projects(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    make_project(root, "node_modules/inner", {"package.json": package_json(scripts={"test": "x"})})
    make_project(root, "app/.venv/lib", {"pyproject.toml": "[project]\nname='x'\n"})
    make_project(root, "app", {"package.json": package_json(scripts={"test": "jest"})})

    report = detect.scan_roots([root], max_depth=4, max_projects=50, seconds=5.0)
    paths = [project["path"] for project in report["projects"]]
    app_path = (root / "app").resolve()
    assert paths == [str(app_path)]
    assert all("node_modules" not in path for path in paths)
    assert str(app_path) in paths


def test_env_file_names_are_listed_but_values_are_never_read(tmp_path):
    secret = "sk-live-do-not-index-0123456789"
    project = make_project(
        tmp_path,
        "secrets",
        {
            "package.json": package_json(scripts={"test": "jest"}),
            ".env": f"API_KEY={secret}\nPORT=9999\n",
            ".env.example": "API_KEY=\n",
        },
    )
    card = detect.detect_dir(project)
    assert card["env_files"] == [".env", ".env.example"]
    blob = json.dumps(card)
    assert secret not in blob
    assert "9999" not in blob  # values are not lifted into ports either


def test_git_branch_and_last_commit_come_from_dot_git_files(tmp_path):
    project = make_project(tmp_path, "hist", {"pyproject.toml": "[project]\nname='hist'\n"})
    gitify(project, branch="trunk", timestamp=1762300000)
    card = detect.detect_dir(project)
    assert card["default_branch"] == "trunk"
    assert card["last_commit_ts"] == 1762300000
    assert card["last_commit"].endswith("Z")


def test_readme_title_is_the_first_heading(tmp_path):
    project = make_project(
        tmp_path,
        "titled",
        {
            "package.json": package_json(scripts={"test": "jest"}),
            "README.md": "<p align=\"center\"><img src=\"banner.png\"></p>\n\n# Demo Project\n\nText.\n",
        },
    )
    card = detect.detect_dir(project)
    assert card["readme_title"] == "Demo Project"


def test_compose_services_and_ports_are_read(tmp_path):
    compose = (
        "services:\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    ports:\n"
        "      - \"5432:5432\"\n"
        "  web:\n"
        "    build: .\n"
        "    ports:\n"
        "      - 8080:80\n"
    )
    project = make_project(tmp_path, "stack", {"compose.yaml": compose, "package.json": package_json(scripts={"test": "jest"})})
    card = detect.detect_dir(project)
    assert [service["name"] for service in card["services"]] == ["db", "web"]
    assert {port["port"] for port in card["ports"]} == {5432, 8080}
    tasks = _tasks(card)
    assert tasks["run"]["command"] == "docker compose up"
    assert "db, web" in tasks["run"]["reason"]


def test_every_command_carries_a_reason_and_a_known_task(tmp_path):
    project = make_project(
        tmp_path,
        "full",
        {
            "pyproject.toml": PYPROJECT_UV,
            "uv.lock": "",
            "package.json": package_json(scripts={"test": "vitest", "build": "vite build"}),
            "pnpm-lock.yaml": "",
            "Makefile": "deploy:\n\t@echo deploying\n",
        },
    )
    card = detect.detect_dir(project)
    assert card["stacks"] == ["python", "node", "make"]
    assert card["commands"]
    allowed = {"install", "test", "lint", "typecheck", "build", "format", "run"}
    for command in card["commands"]:
        assert command["task"] in allowed
        assert command["reason"].strip(), command
        assert command["command"].strip() == command["command"]


def test_detection_reports_non_projects(tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "ideas.txt").write_text("not a project\n")
    assert detect.detect_dir(plain) is None
    assert detect.detect_chain(plain) is None


def test_detect_chain_walks_up_to_the_project_root(tmp_path):
    project = make_project(
        tmp_path,
        "repo",
        {"pyproject.toml": PYPROJECT_UV, "uv.lock": "", "tests/test_x.py": ""},
    )
    nested = project / "src" / "pkg"
    nested.mkdir(parents=True)
    card = detect.detect_chain(nested)
    assert card is not None
    assert Path(card["path"]) == project.resolve()


def test_broken_package_json_still_yields_a_card(tmp_path):
    project = make_project(tmp_path, "broken", {"package.json": "{not json at all"})
    card = detect.detect_dir(project)
    assert card is not None
    assert card["stack"] == "node"
    assert card["package_manager"] == "npm"
    assert any("could not be parsed" in note for note in card["notes"])
    assert "test" not in _tasks(card)


def test_git_dir_file_worktree_is_followed(tmp_path):
    real = tmp_path / "real-git"
    (real / "refs" / "heads").mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/worktree-branch\n", encoding="utf-8")
    project = make_project(tmp_path, "wt", {"package.json": package_json(scripts={"test": "jest"})})
    (project / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    card = detect.detect_dir(project)
    assert card["default_branch"] == "worktree-branch"


def test_compose_does_not_mistake_volumes_or_env_for_ports(tmp_path):
    compose = (
        "services:\n"
        "  web:\n"
        "    image: node:22\n"
        "    ports:\n"
        "      - \"127.0.0.1:8000:8000\"\n"
        "    volumes:\n"
        "      - 3000:3000\n"
        "    environment:\n"
        "      - PORT=9999\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    ports:\n"
        "      - target: 5432\n"
        "        published: \"15432\"\n"
    )
    project = make_project(tmp_path, "svc", {"compose.yaml": compose, "package.json": package_json(scripts={"test": "jest"})})
    card = detect.detect_dir(project)
    ports = {port["port"] for port in card["ports"]}
    assert ports == {8000, 15432}
    assert {service["name"]: service["ports"] for service in card["services"]} == {
        "web": [8000],
        "db": [15432],
    }


def test_requirements_project_with_a_venv_uses_that_interpreter(tmp_path):
    project = make_project(
        tmp_path,
        "venvish",
        {
            "requirements.txt": "pytest==8\n",
            ".venv/bin/python": "",
            ".venv/bin/pytest": "",
        },
    )
    card = detect.detect_dir(project)
    assert card["package_manager"] == "pip"
    assert card["package_manager_reason"] == "requirements.txt"
    tasks = _tasks(card)
    interpreter = f"{project.resolve()}/.venv/bin/python"
    assert tasks["install"]["command"] == f"{interpreter} -m pip install -r requirements.txt"
    assert tasks["test"]["command"] == f"{interpreter} -m pytest"


def test_console_script_uses_the_venv_wrapper_when_present(tmp_path):
    project = make_project(
        tmp_path,
        "cli",
        {
            "pyproject.toml": "[project]\nname = \"cli\"\nversion = \"0.1.0\"\n\n[project.scripts]\ncli = \"cli.main:main\"\n",
            ".venv/bin/python": "",
            ".venv/bin/cli": "",
        },
    )
    card = detect.detect_dir(project)
    assert _tasks(card)["run"]["command"] == f"{project.resolve()}/.venv/bin/cli"
