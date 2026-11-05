"""The read-only promise, tested rather than asserted in prose.

If any detector ever grows a ``subprocess.run`` or an ``os.system``, the first
test fails even though detection still "works". The second checks the other half:
the scanned tree is byte-for-byte unchanged afterwards.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from conftest import make_project, package_json

from atlas import detect, store


@pytest.fixture()
def no_execution(monkeypatch):
    """Make every process-spawning entry point explode."""
    def explode(*args, **kwargs):
        raise AssertionError(f"atlas tried to execute a process: {args[:1]!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, explode)
    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "fork", "forkpty"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, explode)


def _rich_project(tmp_path: Path) -> Path:
    return make_project(
        tmp_path / "code",
        "rich",
        {
            "pyproject.toml": (
                "[project]\nname = \"rich\"\ndependencies = [\"pytest>=8\"]\n\n"
                "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
            ),
            "uv.lock": "version = 1\n",
            "package.json": package_json(scripts={"test": "vitest run", "dev": "vite --port 5173"}),
            "pnpm-lock.yaml": "",
            "Cargo.toml": "[package]\nname = \"rich\"\n",
            "src/main.rs": "fn main() {}\n",
            "go.mod": "module example.com/rich\n",
            "Makefile": "test:\n\t@echo test\n",
            "docker-compose.yml": "services:\n  db:\n    image: postgres\n",
            "README.md": "# Rich\n",
            "tests/test_x.py": "",
        },
    )


def test_detection_spawns_no_process(tmp_path, no_execution, settings_factory):
    project = _rich_project(tmp_path)
    card = detect.detect_dir(project)
    assert card is not None
    report = detect.scan_roots([tmp_path / "code"], max_depth=3, max_projects=10, seconds=5.0)
    assert report["projects"]
    store.ensure_index(settings_factory([tmp_path / "code"]))


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    snapshot: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        snapshot[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, digest)
    return snapshot


def test_scanning_leaves_the_project_untouched(tmp_path, settings_factory):
    root = tmp_path / "code"
    project = _rich_project(tmp_path)
    before = _snapshot(project)
    store.ensure_index(settings_factory([root]))
    store.resolve_cwd(settings_factory([root]), project / "src")
    assert _snapshot(project) == before
    assert not any(path.endswith("index.db") for path in _snapshot(project))


def test_index_lives_under_the_profile_not_the_project(tmp_path, hermes_home, settings_factory):
    root = tmp_path / "code"
    project = _rich_project(tmp_path)
    store.ensure_index(settings_factory([root]))
    db = store.db_path()
    assert db.is_file()
    assert hermes_home in db.parents
    assert project.resolve() not in db.resolve().parents
    assert "plugin-data" in str(db) and "atlas" in str(db)


def test_env_values_never_reach_the_database(tmp_path, hermes_home, settings_factory):
    root = tmp_path / "code"
    secret = "sk-live-9f8e7d6c5b4a-not-for-storage"
    make_project(
        root,
        "app",
        {
            "package.json": package_json(scripts={"test": "jest"}),
            ".env.local": f"DATABASE_URL=postgres://user:{secret}@localhost/db\n",
        },
    )
    store.ensure_index(settings_factory([root]))
    payload = b""
    for candidate in (store.db_path(), store.db_path().with_name("index.db-wal")):
        if candidate.is_file():
            payload += candidate.read_bytes()
    assert secret.encode() not in payload
    assert b".env.local" in payload
