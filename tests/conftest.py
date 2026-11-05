"""Shared harness: throwaway projects on disk, a throwaway HERMES_HOME.

Every test builds the marker files it talks about in a temp directory, so the
detectors are exercised against real files (real scandir, real tomllib, real
SQLite) rather than against mocks of them. Nothing here copies or executes a
project.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_SRC", "/root/.hermes/hermes-agent"))


def pytest_configure(config):
    # ``atlas`` is imported as a package: its parent must be importable, and the
    # Hermes source is needed for the manifest/register contract tests.
    for path in (str(HERMES_SRC), str(REPO_ROOT.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """A private profile dir, so the index never touches the developer's own."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import atlas

    atlas.reset_caches()
    yield home
    atlas.reset_caches()


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_project(root: Path, name: str = "proj", files: dict[str, str | None] | None = None) -> Path:
    """Create ``root/name`` with the given files (``None`` means an empty file)."""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {}).items():
        write(path / relative, "" if content is None else content)
    return path


def package_json(*, scripts: dict[str, str] | None = None, workspaces: bool = False) -> str:
    payload: dict[str, object] = {"name": "fixture", "version": "1.0.0"}
    if scripts is not None:
        payload["scripts"] = scripts
    if workspaces:
        payload["workspaces"] = ["packages/*"]
    return json.dumps(payload, indent=2) + "\n"


def gitify(path: Path, branch: str = "main", timestamp: int = 1762300000, message: str = "initial") -> None:
    """Write the .git files atlas reads — no git process involved."""
    write(path / ".git" / "HEAD", f"ref: refs/heads/{branch}\n")
    write(
        path / ".git" / "logs" / "HEAD",
        "0000000000000000000000000000000000000000 "
        "1111111111111111111111111111111111111111 "
        f"Fixture <fixture@example.com> {timestamp} +0000\tcommit: {message}\n",
    )
    write(path / ".git" / "refs" / "heads" / branch, "1" * 40 + "\n")


@pytest.fixture()
def settings_factory(hermes_home):
    """Settings bound to explicit roots, with a fast, deterministic scan."""
    from atlas.settings import Settings

    def build(roots, **overrides):
        base = dict(
            enabled=True,
            roots=tuple(Path(root) for root in roots),
            max_depth=3,
            max_projects=500,
            scan_seconds=5.0,
            cache_ttl_minutes=60,
            inject_prompt_section=True,
        )
        base.update(overrides)
        return Settings(**base)

    return build


class RecordingContext:
    """The slice of PluginContext that atlas uses, recording instead of registering.

    ``register_hook`` is deliberately present and records: atlas declares no
    hooks, and the manifest test asserts that this list stays empty.
    """

    plugin_id = "atlas"
    profile_name = "default"

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self.tools: dict[str, dict] = {}
        self.commands: dict[str, object] = {}
        self.sections: dict[str, tuple] = {}
        self.hooks: list[str] = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name=None, toolset=None, schema=None, handler=None, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_command(self, name, handler, description="", args_hint="", **kwargs):
        self.commands[name] = handler

    def register_system_prompt_section(self, id, content, *, position="after_memory", max_chars=900):
        self.sections[id] = (content, position, max_chars)

    def register_hook(self, hook_name, callback):
        self.hooks.append(str(hook_name))


@pytest.fixture()
def bind_config(hermes_home):
    """Point atlas at a config (roots, limits) without a full register()."""
    import atlas

    def build(**config):
        ctx = RecordingContext(config)
        atlas._CTX = ctx
        atlas.reset_caches()
        return ctx

    yield build
    atlas._CTX = None
    atlas.reset_caches()


@pytest.fixture()
def register_ctx(hermes_home):
    """Register atlas against a recording context, with reset on teardown."""
    import atlas

    def build(**config):
        ctx = RecordingContext(config)
        atlas.register(ctx)
        atlas.reset_caches()
        return ctx

    yield build
    atlas.reset_caches()
