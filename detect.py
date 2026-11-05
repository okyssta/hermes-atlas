"""Marker-file detection. Nothing here executes a single byte of the project.

The whole module is a set of "look, don't run" probes:

* a directory is a project when at least one *marker file* is in it —
  ``pyproject.toml``, ``package.json``, ``Cargo.toml``, ``go.mod``, …
* a command is emitted only when a marker *declares* it — a ``package.json``
  script, a Makefile target, a pytest config section, a compose service. If the
  marker is absent, no command is guessed and the project card says so under
  ``notes`` instead.
* ``git`` metadata comes from reading ``.git/HEAD`` and ``.git/logs/HEAD``,
  never from spawning ``git``.
* env files are listed by *name*. Their contents are never opened, so a value
  that ends up in ``.env`` cannot end up in the index.

Every reader is bounded (64 KiB) and every failure is swallowed: a project with
a corrupt ``package.json`` still gets a card with the parts that parsed.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # Python >= 3.11; the plugin runs on 3.14 but tests may import standalone
    import tomllib
except Exception:  # pragma: no cover - defensive
    tomllib = None  # type: ignore[assignment]

MAX_READ = 64 * 1024

# Directory names never descended into, and never treated as projects. These are
# build output, vendored code or virtualenvs: scanning them is slow and every hit
# inside them is a false positive.
IGNORED_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        ".git",
        "vendor",
        "__pycache__",
        ".next",
        ".cache",
        "site-packages",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".idea",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        "coverage",
        "htmlcov",
    }
)

# Files that make a directory a project root, in display order.
MARKER_FILES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "uv.lock",
    "poetry.lock",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "Makefile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "manage.py",
)

# Marker directories. ``.venv`` is deliberately *not* enough on its own to call a
# directory a project (a stray venv in a scratch folder is not a project), but
# ``.venv/pyvenv.cfg`` next to python files is treated as a python marker.

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

ENV_PREFIX = ".env"

STACK_ORDER = ("python", "node", "rust", "go", "java", "ruby", "php", "docker-compose", "make")

_MAKE_TASK_MAP = {
    "install": "install",
    "setup": "install",
    "deps": "install",
    "bootstrap": "install",
    "test": "test",
    "tests": "test",
    "check": "lint",
    "lint": "lint",
    "fmt": "format",
    "format": "format",
    "build": "build",
    "run": "run",
    "dev": "run",
    "serve": "run",
    "start": "run",
}


# ── bounded readers ──────────────────────────────────────────────────────────


def read_text(path: Path, limit: int = MAX_READ) -> str:
    """Read at most *limit* bytes of a text file; ``""`` on any failure."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit)
    except (OSError, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path, 512 * 1024))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover
        return {}
    try:
        with open(path, "rb") as handle:
            data = tomllib.loads(handle.read(MAX_READ).decode("utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def scan_names(path: Path) -> dict[str, bool]:
    """``{entry name: is_dir}`` for one directory, without following symlinks."""
    names: dict[str, bool] = {}
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    names[entry.name] = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
    except (OSError, ValueError):
        return {}
    return names


def _first_line_heading(text: str) -> str:
    """First ATX heading, else the first setext heading, in the first 60 lines."""
    lines = [line.strip() for line in text.splitlines()[:60]]
    for line in lines:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title[:160]
    for index, line in enumerate(lines[:-1]):
        if not line or line.startswith("<"):
            continue
        underline = lines[index + 1]
        if len(underline) >= 3 and not set(underline) - set("=-~^"):
            return line[:160]
    return ""


def readme_title(path: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "readme.md", "Readme.md", "README"):
        candidate = path / name
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        title = _first_line_heading(read_text(candidate, 16 * 1024))
        if title:
            return title
    return ""


def env_file_names(path: Path) -> list[str]:
    """Names of ``.env*`` files at the project root. Values are never read."""
    found: list[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.name.startswith(ENV_PREFIX):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        found.append(entry.name)
                except OSError:
                    continue
    except (OSError, ValueError):
        return []
    return sorted(found)[:8]


def layout(path: Path, names: dict[str, bool] | None = None) -> list[str]:
    """A bounded summary of the top level: directories first, then files."""
    names = names if names is not None else scan_names(path)
    kept = [name for name in names if name not in IGNORED_DIRS and not name.startswith(".DS_Store")]
    dirs = sorted((name for name in kept if names[name]), key=str.lower)
    files = sorted((name for name in kept if not names[name]), key=str.lower)
    ordered = [f"{name}/" for name in dirs] + files
    return ordered[:12]


# ── git, read as files ───────────────────────────────────────────────────────


def git_dir(path: Path) -> Path | None:
    dot_git = path / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            content = read_text(dot_git, 4096).strip()
            if content.startswith("gitdir:"):
                target = content.split(":", 1)[1].strip()
                resolved = (path / target).resolve()
                if resolved.is_dir():
                    return resolved
    except OSError:
        return None
    return None


_REF_PREFIX = "ref: "


def git_info(path: Path) -> dict[str, Any]:
    """Branch, default branch and last-commit time, from ``.git`` files only."""
    info: dict[str, Any] = {
        "default_branch": "",
        "branch": "",
        "last_commit": "",
        "last_commit_ts": 0,
    }
    gd = git_dir(path)
    if gd is None:
        return info
    head = read_text(gd / "HEAD", 512).strip()
    branch = ""
    if head.startswith(_REF_PREFIX):
        ref = head[len(_REF_PREFIX) :].strip()
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/") :]
        elif ref.startswith("refs/"):
            branch = ref[len("refs/") :]
    elif head:
        branch = f"detached at {head[:8]}"
    info["branch"] = branch
    info["default_branch"] = branch

    # origin/HEAD is a symref file core writes on clone; it names the branch the
    # remote considers default, which is what "default branch" means to a human.
    origin = read_text(gd / "refs" / "remotes" / "origin" / "HEAD", 512).strip()
    if origin.startswith(_REF_PREFIX):
        ref = origin[len(_REF_PREFIX) :].strip()
        marker = "refs/remotes/origin/"
        if ref.startswith(marker):
            info["default_branch"] = ref[len(marker) :]

    stamp = _last_log_timestamp(gd / "logs" / "HEAD")
    if not stamp and branch:
        stamp = _last_log_timestamp(gd / "logs" / "refs" / "heads" / branch)
    if stamp:
        info["last_commit_ts"] = stamp
        info["last_commit"] = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


_LOG_STAMP = re.compile(r">\s+(\d{9,})\s+[+-]\d{4}")


def _last_log_timestamp(log: Path) -> int:
    text = read_text(log, MAX_READ)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0
    match = _LOG_STAMP.search(lines[-1])
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover
        return 0


# ── command helpers ──────────────────────────────────────────────────────────


def _command(task: str, command: str, reason: str) -> dict[str, str]:
    return {"task": task, "command": " ".join(command.split()), "reason": reason}


def _add(commands: list[dict[str, str]], task: str, command: str, reason: str) -> None:
    command = " ".join(str(command).split())
    if not command:
        return
    for existing in commands:
        if existing["command"] == command:
            return
    commands.append(_command(task, command, reason))


def _has_task(commands: Iterable[dict[str, str]], task: str) -> bool:
    return any(item["task"] == task for item in commands)


def _clean_port(value: str) -> int | None:
    try:
        port = int(str(value).strip().strip("\"'"))
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


# ── stack detectors ──────────────────────────────────────────────────────────


def _pyproject_info(path: Path) -> dict[str, Any]:
    return read_toml(path / "pyproject.toml")


def _requirements_text(path: Path) -> str:
    for name in ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt"):
        text = read_text(path / name, 8192)
        if text:
            return text
    return ""


def _python_test_command(path: Path, names: dict[str, bool], pyproject: dict[str, Any]) -> tuple[str, str] | None:
    """The pytest/Django invocation plus the marker that justifies it."""
    tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
    if isinstance(tool.get("pytest"), dict):
        return "pytest", "pyproject.toml [tool.pytest.ini_options]"
    if "pytest.ini" in names:
        return "pytest", "pytest.ini"
    tox = read_text(path / "tox.ini", 8192)
    if "[pytest]" in tox:
        return "pytest", "tox.ini [pytest]"
    setup_cfg = read_text(path / "setup.cfg", 8192)
    if "[tool:pytest]" in setup_cfg:
        return "pytest", "setup.cfg [tool:pytest]"
    deps = ""
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    for key in ("dependencies", "optional-dependencies"):
        value = project.get(key)
        if isinstance(value, list):
            deps += " ".join(str(item) for item in value)
        elif isinstance(value, dict):
            for items in value.values():
                if isinstance(items, list):
                    deps += " ".join(str(item) for item in items)
    deps += " " + _requirements_text(path)
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    for group in ("dependencies", "dev-dependencies", "group"):
        value = poetry.get(group)
        if isinstance(value, dict):
            deps += " " + " ".join(str(key) for key in value)
    if re.search(r"(^|[\s\"'=<>\[,])pytest([\s\"'<>=,\]]|$)", deps, re.I):
        return "pytest", "pytest listed in dependencies"
    if "conftest.py" in names:
        return "pytest", "conftest.py at project root"
    if _venv_script(path, "pytest"):
        return "pytest", "pytest installed in the project venv"
    if "tests" in names and "manage.py" not in names:
        # tests/ alone is weak evidence; only claim it when a tests package with
        # pytest-style files is actually there.
        try:
            for entry in os.scandir(path / "tests"):
                if entry.name.startswith("test_") and entry.name.endswith(".py"):
                    return "pytest", "tests/test_*.py files"
        except OSError:
            pass
    if "manage.py" in names:
        return "django", "manage.py"
    return None


def _venv_python(path: Path) -> Path | None:
    """The interpreter inside a project venv, on POSIX or Windows layouts."""
    for venv in (".venv", "venv", "env"):
        for relative in (Path("bin") / "python", Path("Scripts") / "python.exe"):
            candidate = path / venv / relative
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
    return None


def _venv_script(path: Path, name: str) -> bool:
    for venv in (".venv", "venv"):
        for relative in (Path("bin") / name, Path("Scripts") / f"{name}.exe"):
            try:
                if (path / venv / relative).exists():
                    return True
            except OSError:
                continue
    return False


def _python_runner(path: Path, names: dict[str, bool], pm: str, has_project: bool) -> tuple[str, str]:
    """(prefix for the interpreter, how it was chosen)."""
    if pm == "uv" and has_project:
        return "uv run ", "uv project"
    if pm == "poetry":
        return "poetry run ", "poetry project"
    if pm == "pipenv":
        return "pipenv run ", "Pipfile"
    venv_python = _venv_python(path)
    if venv_python is not None:
        # Absolute on purpose: the session's cwd may be a subdirectory of the
        # project, and a relative `.venv/bin/python` would silently miss.
        return f"{venv_python.as_posix()} -m ", str(venv_python.relative_to(path))
    return "python -m ", "system python"


def _detect_python(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    pyproject = _pyproject_info(path)
    venv_cfg = read_text(path / ".venv" / "pyvenv.cfg", 2048)
    has_pyproject = "pyproject.toml" in names
    has_setup_py = "setup.py" in names
    has_requirements = "requirements.txt" in names
    has_pipfile = "Pipfile" in names
    has_poetry_lock = "poetry.lock" in names
    has_uv_lock = "uv.lock" in names
    has_venv = bool(venv_cfg) or ".venv" in names or "venv" in names

    poetry_table = pyproject.get("tool", {}).get("poetry") if isinstance(pyproject.get("tool"), dict) else None
    looks_python = bool(
        has_pyproject
        or has_setup_py
        or "setup.cfg" in names
        or has_requirements
        or has_pipfile
        or has_poetry_lock
        or has_uv_lock
        or "manage.py" in names
        or (has_venv and any(name.endswith(".py") for name in names))
    )
    if not looks_python:
        return None

    if has_uv_lock or (not has_poetry_lock and "uv = " in venv_cfg):
        pm = "uv"
        pm_reason = "uv.lock" if has_uv_lock else ".venv/pyvenv.cfg created by uv"
    elif has_poetry_lock or isinstance(poetry_table, dict):
        pm = "poetry"
        pm_reason = "poetry.lock" if has_poetry_lock else "pyproject.toml [tool.poetry]"
    elif has_pipfile:
        pm = "pipenv"
        pm_reason = "Pipfile"
    elif has_pyproject or has_setup_py or "setup.cfg" in names or has_requirements:
        pm = "pip"
        if has_pyproject:
            pm_reason = "pyproject.toml"
        elif has_setup_py:
            pm_reason = "setup.py"
        elif "setup.cfg" in names:
            pm_reason = "setup.cfg"
        else:
            pm_reason = "requirements.txt"
    else:
        pm = "venv"
        pm_reason = ".venv present, no dependency manifest"

    commands: list[dict[str, str]] = []
    notes: list[str] = []
    prefix, prefix_reason = _python_runner(path, names, pm, has_pyproject or has_uv_lock)
    # When a venv exists but no manager declares the install, go through that
    # interpreter: a bare `pip install` can easily be the wrong pip.
    pip = f"{prefix}pip" if _venv_python(path) is not None else "pip"

    if pm == "uv" and has_pyproject:
        _add(commands, "install", "uv sync", "uv.lock present" if has_uv_lock else "pyproject.toml + uv")
    elif pm == "poetry":
        _add(commands, "install", "poetry install", pm_reason)
    elif pm == "pipenv":
        pipfile = read_text(path / "Pipfile", 8192)
        command = "pipenv install --dev" if "[dev-packages]" in pipfile else "pipenv install"
        _add(commands, "install", command, "Pipfile")
    elif has_requirements:
        _add(commands, "install", f"{pip} install -r requirements.txt", "requirements.txt")
    elif has_pyproject or has_setup_py or "setup.cfg" in names:
        _add(commands, "install", f"{pip} install -e .", pm_reason)
    else:
        notes.append("no install command: no dependency manifest at the project root")

    test = _python_test_command(path, names, pyproject)
    if test is not None:
        kind, why = test
        if kind == "django":
            _add(commands, "test", f"{prefix}manage.py test", "manage.py (Django test runner)")
        else:
            _add(commands, "test", f"{prefix}pytest", why)
    else:
        notes.append("no test command: no pytest config, conftest.py or Django manage.py to derive one from")

    tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
    has_ruff = isinstance(tool.get("ruff"), dict) or "ruff.toml" in names or ".ruff.toml" in names
    has_flake8 = ".flake8" in names or "[flake8]" in read_text(path / "setup.cfg", 8192)
    has_mypy = isinstance(tool.get("mypy"), dict) or "mypy.ini" in names or ".mypy.ini" in names
    has_black = isinstance(tool.get("black"), dict)
    if has_ruff:
        _add(commands, "lint", f"{prefix}ruff check .", "ruff config in pyproject.toml/ruff.toml")
        _add(commands, "format", f"{prefix}ruff format .", "ruff config in pyproject.toml/ruff.toml")
    elif has_flake8:
        _add(commands, "lint", f"{prefix}flake8", "flake8 config (.flake8/setup.cfg)")
    elif has_black:
        _add(commands, "lint", f"{prefix}black --check .", "pyproject.toml [tool.black]")
    if has_black and not has_ruff:
        _add(commands, "format", f"{prefix}black .", "pyproject.toml [tool.black]")
    if has_mypy:
        _add(commands, "typecheck", f"{prefix}mypy .", "mypy config")

    build_system = pyproject.get("build-system")
    if isinstance(build_system, dict):
        if pm == "uv" and has_pyproject:
            _add(commands, "build", "uv build", "pyproject.toml [build-system]")
        elif pm == "poetry":
            _add(commands, "build", "poetry build", "pyproject.toml [build-system]")
        else:
            _add(commands, "build", "python -m build", "pyproject.toml [build-system]")

    if "manage.py" in names:
        _add(commands, "run", f"{prefix}manage.py runserver", "manage.py (Django dev server)")
    project_table = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    scripts = project_table.get("scripts") if isinstance(project_table.get("scripts"), dict) else {}
    if scripts and not _has_task(commands, "run"):
        console = sorted(str(name) for name in scripts)[0]
        _add(commands, "run", _console_command(path, console), f"pyproject.toml [project.scripts] entry '{console}'")
    if not _has_task(commands, "run"):
        for package in _python_main_packages(path, names):
            _add(commands, "run", f"{prefix}{package}", f"{package}/__main__.py")
            break

    ports: list[dict[str, Any]] = []
    ports.extend(_ports_from_text(" ".join(scripts.values()), "pyproject.toml [project.scripts]"))

    return {
        "stack": "python",
        "package_manager": pm,
        "package_manager_reason": pm_reason,
        "commands": commands,
        "ports": ports,
        "notes": notes,
        "runner": prefix_reason,
    }


def _console_command(path: Path, name: str) -> str:
    """A ``[project.scripts]`` entry as something runnable.

    Inside a venv the installed wrapper is exact; otherwise the entry name is
    what an installed package puts on PATH.
    """
    venv_python = _venv_python(path)
    if venv_python is not None:
        for relative in (venv_python.parent / name, venv_python.parent / f"{name}.exe"):
            try:
                if relative.exists():
                    return relative.as_posix()
            except OSError:
                continue
    return name


def _python_main_packages(path: Path, names: dict[str, bool]) -> list[str]:
    """Shallowest importable package that ships ``__main__.py``."""
    found: list[str] = []
    candidates: list[tuple[str, Path]] = []
    for name, is_dir in names.items():
        if is_dir and name not in IGNORED_DIRS and not name.startswith("."):
            candidates.append((name, path / name))
    src = path / "src"
    if src.is_dir():
        try:
            for entry in os.scandir(src):
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                    candidates.append((entry.name, Path(entry.path)))
        except OSError:
            pass
    for name, directory in candidates:
        try:
            if (directory / "__main__.py").is_file():
                found.append(name)
        except OSError:
            continue
    return found[:1]


def _ports_from_text(text: str, source: str) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    seen: set[int] = set()
    for match in re.finditer(r"(?:--port[= ]|-p\s?|PORT=)(\d{2,5})", text):
        port = _clean_port(match.group(1))
        if port and port not in seen:
            seen.add(port)
            ports.append({"port": port, "source": source})
    return ports


def _detect_node(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    if "package.json" not in names:
        return None
    package = read_json(path / "package.json")
    notes: list[str] = []
    if not package:
        notes.append("package.json exists but could not be parsed as JSON; scripts unknown")

    if "pnpm-lock.yaml" in names:
        pm, pm_reason = "pnpm", "pnpm-lock.yaml"
    elif "yarn.lock" in names:
        pm, pm_reason = "yarn", "yarn.lock"
    elif "bun.lockb" in names or "bun.lock" in names:
        pm, pm_reason = "bun", "bun.lockb"
    elif "package-lock.json" in names:
        pm, pm_reason = "npm", "package-lock.json"
    else:
        pm, pm_reason = "npm", "no lockfile, npm default"

    commands: list[dict[str, str]] = []
    _add(commands, "install", f"{pm} install", pm_reason)

    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    scripts = {str(key): str(value) for key, value in scripts.items() if isinstance(value, str)}
    task_map = {
        "test": "test",
        "lint": "lint",
        "typecheck": "typecheck",
        "build": "build",
        "bundle": "build",
        "dev": "run",
        "start": "run",
        "serve": "run",
        "format": "format",
        "fmt": "format",
    }
    for script, task in task_map.items():
        if script in scripts and not _has_task(commands, task):
            _add(commands, task, f"{pm} run {script}", f"package.json scripts.{script}")

    if not _has_task(commands, "test"):
        notes.append("no test command: package.json has no scripts.test")

    ports = _ports_from_text(" ".join(scripts.values()), "package.json scripts")

    if package.get("workspaces"):
        notes.append("npm/yarn workspaces declared; sub-packages are indexed separately when they have their own manifest")

    return {
        "stack": "node",
        "package_manager": pm,
        "package_manager_reason": pm_reason,
        "commands": commands,
        "ports": ports,
        "notes": notes,
    }


def _detect_rust(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    if "Cargo.toml" not in names:
        return None
    manifest = read_toml(path / "Cargo.toml")
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    _add(commands, "install", "cargo fetch", "Cargo.toml")
    _add(commands, "build", "cargo build", "Cargo.toml")
    _add(commands, "test", "cargo test", "Cargo.toml (cargo's built-in test runner)")
    _add(commands, "lint", "cargo clippy --all-targets", "Cargo.toml (clippy ships with the Rust toolchain)")
    if "rustfmt.toml" in names or ".rustfmt.toml" in names:
        _add(commands, "format", "cargo fmt --check", "rustfmt.toml")
    has_main = "src/main.rs" in _rel_files(path, "src") or (path / "src" / "main.rs").is_file()
    bins = manifest.get("bin") if isinstance(manifest.get("bin"), list) else []
    package = manifest.get("package") if isinstance(manifest.get("package"), dict) else {}
    if bins or has_main:
        _add(commands, "run", "cargo run", "src/main.rs" if has_main else "Cargo.toml [[bin]]")
    elif not package.get("name"):
        notes.append("Cargo.toml has no [package] name; the manifest is probably a workspace root")
    return {
        "stack": "rust",
        "package_manager": "cargo",
        "package_manager_reason": "Cargo.toml",
        "commands": commands,
        "ports": [],
        "notes": notes,
    }


def _rel_files(path: Path, sub: str) -> set[str]:
    try:
        with os.scandir(path / sub) as entries:
            return {f"{sub}/{entry.name}" for entry in entries if entry.is_file()}
    except OSError:
        return set()


def _detect_go(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    if "go.mod" not in names:
        return None
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    module = ""
    for line in read_text(path / "go.mod", 8192).splitlines()[:20]:
        if line.strip().startswith("module "):
            module = line.strip().split(None, 1)[1].strip()
            break
    _add(commands, "install", "go mod download", "go.mod")
    _add(commands, "build", "go build ./...", "go.mod")
    _add(commands, "test", "go test ./...", "go.mod (go test ships with the toolchain)")
    _add(commands, "lint", "go vet ./...", "go.mod (go vet ships with the toolchain)")

    try:
        root_main = (path / "main.go").is_file()
    except OSError:
        root_main = False
    cmd_dirs: list[str] = []
    try:
        with os.scandir(path / "cmd") as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and (Path(entry.path) / "main.go").is_file():
                    cmd_dirs.append(entry.name)
    except OSError:
        pass
    if root_main:
        _add(commands, "run", "go run .", "main.go at the module root")
    elif cmd_dirs:
        target = sorted(cmd_dirs)[0]
        _add(commands, "run", f"go run ./cmd/{target}", f"cmd/{target}/main.go")
    else:
        notes.append("no run command: no main package at the root or under cmd/")
    if module:
        notes.append(f"module {module}")
    return {
        "stack": "go",
        "package_manager": "go modules",
        "package_manager_reason": "go.mod",
        "commands": commands,
        "ports": [],
        "notes": notes,
    }


def _detect_java(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    maven = "pom.xml" in names
    gradle = "build.gradle" in names or "build.gradle.kts" in names
    if not (maven or gradle):
        return None
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    if maven:
        wrapper = "./mvnw" if "mvnw" in names else "mvn"
        pom = read_text(path / "pom.xml", MAX_READ)
        _add(commands, "build", f"{wrapper} -q package", "pom.xml (Maven)")
        _add(commands, "test", f"{wrapper} -q test", "pom.xml (Maven surefire)")
        if "spring-boot-maven-plugin" in pom:
            _add(commands, "run", f"{wrapper} spring-boot:run", "pom.xml spring-boot-maven-plugin")
        else:
            notes.append("no run command: pom.xml does not declare spring-boot-maven-plugin")
        pm, pm_reason = ("maven wrapper", "mvnw") if wrapper == "./mvnw" else ("maven", "pom.xml")
    else:
        wrapper = "./gradlew" if "gradlew" in names else "gradle"
        build_file = "build.gradle.kts" if "build.gradle.kts" in names else "build.gradle"
        text = read_text(path / build_file, MAX_READ)
        _add(commands, "build", f"{wrapper} build", f"{build_file} (Gradle)")
        _add(commands, "test", f"{wrapper} test", f"{build_file} (Gradle test task)")
        if re.search(r"""id\s*\(?\s*['"]application['"]""", text):
            _add(commands, "run", f"{wrapper} run", f"{build_file} application plugin")
        else:
            notes.append("no run command: the Gradle build does not apply the application plugin")
        pm, pm_reason = ("gradle wrapper", "gradlew") if wrapper == "./gradlew" else ("gradle", build_file)
    return {
        "stack": "java",
        "package_manager": pm,
        "package_manager_reason": pm_reason,
        "commands": commands,
        "ports": [],
        "notes": notes,
    }


def _detect_ruby(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    if "Gemfile" not in names:
        return None
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    _add(commands, "install", "bundle install", "Gemfile")
    if "spec" in names or ".rspec" in names:
        _add(commands, "test", "bundle exec rspec", "spec/ directory" if "spec" in names else ".rspec")
    elif "Rakefile" in names:
        _add(commands, "test", "bundle exec rake test", "Rakefile")
    else:
        notes.append("no test command: no spec/ directory, .rspec or Rakefile")
    if ".rubocop.yml" in names:
        _add(commands, "lint", "bundle exec rubocop", ".rubocop.yml")
    if "bin/rails" in _rel_files(path, "bin"):
        _add(commands, "run", "bundle exec rails server", "bin/rails")
    elif "config.ru" in names:
        _add(commands, "run", "bundle exec rackup", "config.ru")
    return {
        "stack": "ruby",
        "package_manager": "bundler",
        "package_manager_reason": "Gemfile",
        "commands": commands,
        "ports": [],
        "notes": notes,
    }


def _detect_php(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    if "composer.json" not in names:
        return None
    composer = read_json(path / "composer.json")
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    _add(commands, "install", "composer install", "composer.json")
    scripts = composer.get("scripts") if isinstance(composer.get("scripts"), dict) else {}
    if "test" in scripts:
        _add(commands, "test", "composer test", "composer.json scripts.test")
    elif "phpunit.xml" in names or "phpunit.xml.dist" in names:
        _add(commands, "test", "vendor/bin/phpunit", "phpunit.xml")
    else:
        notes.append("no test command: no scripts.test and no phpunit.xml")
    if "artisan" in names:
        _add(commands, "run", "php artisan serve", "artisan (Laravel)")
    return {
        "stack": "php",
        "package_manager": "composer",
        "package_manager_reason": "composer.json",
        "commands": commands,
        "ports": [],
        "notes": notes,
    }


def _compose_file(path: Path, names: dict[str, bool]) -> str:
    for name in COMPOSE_FILES:
        if name in names:
            return name
    return ""


def _host_port(value: str) -> int | None:
    """Host-side port from a compose port scalar ("8080:80", "127.0.0.1:8080:80", "80")."""
    text = str(value).strip().strip("\"'")
    parts = [part for part in text.split(":") if part]
    if not parts:
        return None
    if len(parts) == 1:
        return _clean_port(parts[0])
    return _clean_port(parts[-2]) or _clean_port(parts[0])


def _parse_compose(text: str) -> dict[str, Any]:
    """Service names and host-side ports, parsed line by line.

    Deliberately not a YAML parser: atlas has no dependencies, and only two
    things are needed. A key line inside a service switches the mode, so a
    ``volumes:`` entry that happens to look like ``- 3000:3000`` is not mistaken
    for a published port.
    """
    services: list[dict[str, Any]] = []
    in_services = False
    current: dict[str, Any] | None = None
    in_ports = False
    service_indent: int | None = None
    for raw in text.splitlines()[:4000]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0:
            in_services = stripped.rstrip(":").strip() == "services" and stripped.endswith(":")
            current, in_ports, service_indent = None, False, None
            continue
        if not in_services:
            continue
        if stripped.startswith("- "):
            if current is not None and in_ports:
                port = _host_port(stripped[2:].strip())
                if port and port not in current["ports"]:
                    current["ports"].append(port)
            continue
        if stripped.endswith(":"):
            key = stripped[:-1].strip().strip("\"'")
            if service_indent is None:
                service_indent = indent
            if indent == service_indent and key != "ports":
                current = {"name": key, "ports": []}
                services.append(current)
                in_ports = False
                continue
            if current is not None:
                in_ports = key == "ports"
            continue
        # Long syntax: ``published: "8080"`` under a ``ports:`` entry.
        if current is not None and in_ports and stripped.startswith("published:"):
            port = _clean_port(stripped.split(":", 1)[1].strip().strip("\"'"))
            if port and port not in current["ports"]:
                current["ports"].append(port)
            continue
        # Inline form: ``ports: ["8080:80"]``.
        if current is not None and stripped.startswith("ports:"):
            in_ports = True
            for match in re.finditer(r"""["']?(\d{2,5}):\d{1,5}""", stripped):
                port = _clean_port(match.group(1))
                if port and port not in current["ports"]:
                    current["ports"].append(port)
    return {"services": services}


def _detect_compose(path: Path, names: dict[str, bool]) -> dict[str, Any] | None:
    compose = _compose_file(path, names)
    if not compose:
        return None
    parsed = _parse_compose(read_text(path / compose, MAX_READ))
    services = parsed["services"]
    names_list = [service["name"] for service in services]
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    if names_list:
        _add(commands, "run", "docker compose up", f"{compose} services: {', '.join(names_list[:6])}")
        _add(commands, "build", "docker compose build", f"{compose} services: {', '.join(names_list[:6])}")
    else:
        notes.append(f"{compose} has no services block that could be parsed")
    ports = [
        {"port": port, "source": f"{compose} service '{service['name']}'"}
        for service in services
        for port in service["ports"]
    ]
    return {
        "stack": "docker-compose",
        "package_manager": "docker compose",
        "package_manager_reason": compose,
        "commands": commands,
        "ports": ports,
        "notes": notes,
        "services": services,
    }


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)[ \t]*:(?!=)")


def parse_makefile(text: str) -> list[str]:
    """Top-level target names from a Makefile, parsed without executing it."""
    targets: list[str] = []
    for line in text.splitlines()[:6000]:
        if not line or line[0] in "\t #":
            continue
        if "%" in line.split(":", 1)[0]:
            continue
        match = _MAKE_TARGET.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.startswith(".") or name in targets:
            continue
        targets.append(name)
    return targets


def _detect_make(path: Path, names: dict[str, bool], existing: list[dict[str, str]]) -> dict[str, Any] | None:
    if "Makefile" not in names and "makefile" not in names:
        return None
    file = "Makefile" if "Makefile" in names else "makefile"
    targets = parse_makefile(read_text(path / file, MAX_READ))
    commands: list[dict[str, str]] = []
    for target in targets:
        task = _MAKE_TASK_MAP.get(target)
        if task and not _has_task(existing, task) and not _has_task(commands, task):
            _add(commands, task, f"make {target}", f"{file} target '{target}'")
    notes: list[str] = []
    extra = [target for target in targets if target not in _MAKE_TASK_MAP]
    if extra:
        notes.append(f"{file} also defines: {', '.join(extra[:8])}")
    return {
        "stack": "make",
        "package_manager": "make",
        "package_manager_reason": file,
        "commands": commands,
        "ports": [],
        "notes": notes,
        "targets": targets[:40],
    }


# ── assembly ─────────────────────────────────────────────────────────────────


def is_project_dir(path: Path | str, names: dict[str, bool] | None = None) -> bool:
    path = Path(path)
    names = names if names is not None else scan_names(path)
    if not names:
        return False
    markers = [name for name in MARKER_FILES if name in names and not names[name]]
    if markers:
        return True
    # A python project with only a virtualenv and source files still counts;
    # this is the "someone ran uv venv and wrote code" case.
    try:
        if (path / ".venv" / "pyvenv.cfg").is_file() and any(
            name.endswith(".py") for name, is_dir in names.items() if not is_dir
        ):
            return True
    except OSError:
        return False
    return False


def detect_dir(path: Path | str, names: dict[str, bool] | None = None) -> dict[str, Any] | None:
    """Build the full project card for *path*, or ``None`` if it is not a project."""
    path = Path(path)
    names = names if names is not None else scan_names(path)
    if not is_project_dir(path, names):
        return None

    stacks: list[dict[str, Any]] = []
    for detector in (
        _detect_python,
        _detect_node,
        _detect_rust,
        _detect_go,
        _detect_java,
        _detect_ruby,
        _detect_php,
    ):
        try:
            found = detector(path, names)
        except Exception:  # pragma: no cover - a broken marker must not abort the scan
            found = None
        if found:
            stacks.append(found)

    commands: list[dict[str, str]] = []
    ports: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    notes: list[tuple[str, str]] = []
    for stack in stacks:
        for command in stack.get("commands", []):
            if not any(
                existing["command"] == command["command"] for existing in commands
            ):
                commands.append(command)
        ports.extend(stack.get("ports", []))
        for note in stack.get("notes", []):
            notes.append((str(stack.get("stack") or ""), note))
        services.extend(stack.get("services", []))

    compose = None
    try:
        compose = _detect_compose(path, names)
    except Exception:  # pragma: no cover
        compose = None
    if compose:
        stacks.append(compose)
        for command in compose["commands"]:
            if not any(existing["command"] == command["command"] for existing in commands):
                commands.append(command)
        ports.extend(compose["ports"])
        services.extend(compose.get("services", []))
        for note in compose.get("notes", []):
            notes.append(("docker-compose", note))

    make = None
    try:
        make = _detect_make(path, names, commands)
    except Exception:  # pragma: no cover
        make = None
    if make:
        stacks.append(make)
        commands.extend(make["commands"])
        for note in make.get("notes", []):
            notes.append(("make", note))

    stack_names = [stack["stack"] for stack in stacks]
    ordered = [name for name in STACK_ORDER if name in stack_names]
    if not ordered:  # pragma: no cover - is_project_dir guarantees at least one
        return None

    primary = stacks[0] if stacks else {}
    package_manager = ""
    package_manager_reason = ""
    for stack in stacks:
        if stack["stack"] != "make" or not package_manager:
            package_manager = stack.get("package_manager", "")
            package_manager_reason = stack.get("package_manager_reason", "")
            break

    deduped_ports: list[dict[str, Any]] = []
    seen_ports: set[int] = set()
    for item in ports:
        port = _clean_port(item.get("port"))
        if port and port not in seen_ports:
            seen_ports.add(port)
            deduped_ports.append({"port": port, "source": str(item.get("source") or "")})

    git = git_info(path)
    names_snapshot = names
    try:
        real = os.path.realpath(str(path))
    except OSError:  # pragma: no cover
        real = str(path)

    return {
        "path": real,
        "name": Path(real).name or real,
        "stacks": ordered,
        "stack": ordered[0],
        "stack_label": " + ".join(
            f"{name} ({_pm_for(name, stacks)})" if _pm_for(name, stacks) else name for name in ordered
        ),
        "package_manager": package_manager,
        "package_manager_reason": package_manager_reason,
        "commands": commands,
        "ports": deduped_ports,
        "services": services,
        "env_files": env_file_names(path),
        "default_branch": git["default_branch"],
        "branch": git["branch"],
        "last_commit": git["last_commit"],
        "last_commit_ts": git["last_commit_ts"],
        "readme_title": readme_title(path),
        "layout": layout(path, names_snapshot),
        "markers": _marker_evidence(path, names),
        "notes": _dedupe_text(_keep_notes(notes, ordered[0] if ordered else ""))[:6],
        "primary_stack": primary.get("stack", ""),
        "scanned_at": time.time(),
    }


def _marker_evidence(path: Path, names: dict[str, bool]) -> list[str]:
    """Marker files that made this a project, plus a venv-only python signal."""
    evidence = [name for name in MARKER_FILES if name in names and not names[name]]
    try:
        if (path / ".venv" / "pyvenv.cfg").is_file():
            evidence.append(".venv/pyvenv.cfg")
    except OSError:
        pass
    return evidence[:8]


_NO_COMMAND_NOTE = re.compile(r"^no (test|lint|build|run|install) command", re.I)


def _keep_notes(notes: Iterable[tuple[str, str]], primary: str) -> list[str]:
    """Drop "no X command" notes from secondary stacks — a node workspace root
    inside a python repo should not make the card look broken."""
    kept: list[str] = []
    for stack, note in notes:
        if stack != primary and _NO_COMMAND_NOTE.match(note):
            continue
        kept.append(note)
    return kept


def _pm_for(stack_name: str, stacks: list[dict[str, Any]]) -> str:
    for stack in stacks:
        if stack.get("stack") == stack_name:
            pm = str(stack.get("package_manager") or "")
            return "" if pm in {"make", "docker compose", "go modules"} else pm
    return ""


def _dedupe_text(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def detect_chain(path: Path | str, max_up: int = 6) -> dict[str, Any] | None:
    """Nearest project at or above *path* — the session-cwd lookup."""
    try:
        current = Path(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover
        return None
    try:
        if current.is_file():
            current = current.parent
    except OSError:
        pass
    for _ in range(max_up):
        project = detect_dir(current)
        if project is not None:
            return project
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ── scanning ─────────────────────────────────────────────────────────────────


def scan_roots(
    roots: Iterable[Path | str],
    *,
    max_depth: int = 3,
    max_projects: int = 500,
    seconds: float = 6.0,
    ignored: Iterable[str] = IGNORED_DIRS,
) -> dict[str, Any]:
    """Breadth-first walk of *roots*; returns projects plus a scan report.

    Symlinks are never followed, so a loop cannot hang the walk. The walk stops
    on a wall-clock budget and on the project cap; both are reported so the
    caller can tell a complete index from a partial one.
    """
    ignore = set(ignored)
    started = time.monotonic()
    deadline = started + max(0.1, float(seconds))
    projects: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    visited_dirs: set[tuple[int, int]] = set()
    roots_done: list[str] = []
    roots_skipped: list[dict[str, str]] = []
    dirs_visited = 0
    truncated = False

    queue: list[tuple[Path, int]] = []
    for root in roots:
        path = Path(root)
        try:
            resolved = Path(os.path.realpath(str(path)))
        except OSError:  # pragma: no cover
            resolved = path
        if not resolved.is_dir():
            roots_skipped.append({"root": str(path), "why": "not a directory"})
            continue
        roots_done.append(str(resolved))
        queue.append((resolved, 0))

    index = 0
    cap_hit = False
    while index < len(queue):
        directory, depth = queue[index]
        index += 1
        if time.monotonic() > deadline:
            truncated = True
            break
        if len(projects) >= max_projects:
            cap_hit = True
            break
        try:
            stat = directory.stat()
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in visited_dirs:
            continue
        visited_dirs.add(key)

        names = scan_names(directory)
        if not names:
            continue
        dirs_visited += 1
        if is_project_dir(directory, names):
            try:
                project = detect_dir(directory, names)
            except Exception:  # pragma: no cover
                project = None
            if project is not None and project["path"] not in seen_paths:
                seen_paths.add(project["path"])
                projects.append(project)
        if depth >= max_depth:
            continue
        children: list[str] = []
        for name in names:
            if not names[name] or name in ignore:
                continue
            children.append(name)
        # Ordinary directories first, hidden ones last: when the budget runs out
        # it should run out after the obvious code folders (~/code, ~/src), not
        # after ~/.cache. Hidden folders are still walked because real projects
        # live under them (``~/.config/nvim``, ``~/.hermes/...``).
        children.sort(key=lambda name: (name.startswith("."), name.lower()))
        for name in children:
            child = directory / name
            try:
                if child.is_symlink():
                    continue
            except OSError:
                continue
            queue.append((child, depth + 1))

    elapsed = time.monotonic() - started
    if cap_hit and index < len(queue):
        truncated = True
    return {
        "projects": projects,
        "roots": roots_done,
        "skipped_roots": roots_skipped,
        "dirs_visited": dirs_visited,
        "truncated": truncated,
        "elapsed": round(elapsed, 3),
        "scanned_at": time.time(),
    }
