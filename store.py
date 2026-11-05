"""SQLite index of detected projects, keyed by realpath.

One table holds the card as JSON plus the few columns the list view sorts and
filters on. The database lives under the profile's
``plugin-data/atlas/index.db`` — never inside a scanned project, which keeps the
read-only promise on the scanning side of the boundary too.

Refreshes are explicit (``project_refresh``, ``/atlas refresh``) or lazy: a
lookup sees a row older than ``cache_ttl_minutes`` and re-detects just that
project, while a full root walk happens only when the index is cold/stale and
something asks for a listing. A module lock serialises concurrent refreshes so
two tool calls cannot walk the same tree at once.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import detect
from .settings import PLUGIN_ID, Settings, expand_path

_LOCK = threading.RLock()
_LAST_REPORT: dict[str, Any] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    path            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    stack           TEXT NOT NULL DEFAULT '',
    last_commit_ts  REAL NOT NULL DEFAULT 0,
    scanned_at      REAL NOT NULL DEFAULT 0,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_name_idx ON projects (name);
CREATE INDEX IF NOT EXISTS projects_commit_idx ON projects (last_commit_ts DESC);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - importable outside a Hermes host
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env) if env else Path.home() / ".hermes"


def data_dir() -> Path:
    return hermes_home() / "plugin-data" / PLUGIN_ID


def db_path() -> Path:
    return data_dir() / "index.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # pragma: no cover - read-only or odd filesystem
            pass
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── rows ─────────────────────────────────────────────────────────────────────


def save(project: dict[str, Any]) -> None:
    path = str(project.get("path") or "")
    if not path:
        return
    payload = json.dumps(project, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO projects (path, name, stack, last_commit_ts, scanned_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET name=excluded.name, stack=excluded.stack, "
            "last_commit_ts=excluded.last_commit_ts, scanned_at=excluded.scanned_at, data=excluded.data",
            (
                path,
                str(project.get("name") or Path(path).name),
                str(project.get("stack") or ""),
                float(project.get("last_commit_ts") or 0),
                time.time(),
                payload,
            ),
        )


def _row_to_project(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["data"])
    except (ValueError, TypeError):  # pragma: no cover - corrupt row
        return {}
    return data if isinstance(data, dict) else {}


def get(path: str | Path) -> dict[str, Any] | None:
    try:
        real = os.path.realpath(str(path))
    except OSError:  # pragma: no cover
        real = str(path)
    with _connect() as conn:
        row = conn.execute("SELECT data FROM projects WHERE path = ?", (real,)).fetchone()
    return _row_to_project(row) if row is not None else None


def count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
    return int(row["n"]) if row else 0


def all_projects(query: str = "", limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data FROM projects ORDER BY last_commit_ts DESC, scanned_at DESC, name ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    projects = [project for project in (_row_to_project(row) for row in rows) if project]
    return _filter(projects, query)


def _filter(projects: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    words = [word for word in str(query or "").lower().split() if word]
    if not words:
        return projects
    out: list[dict[str, Any]] = []
    for project in projects:
        haystack = " ".join(
            [
                str(project.get("name") or ""),
                str(project.get("path") or ""),
                str(project.get("stack") or ""),
                str(project.get("stack_label") or ""),
                str(project.get("readme_title") or ""),
                " ".join(project.get("stacks") or []),
            ]
        ).lower()
        if all(word in haystack for word in words):
            out.append(project)
    return out


def clear() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        conn.execute("DELETE FROM projects")
        conn.execute("DELETE FROM meta WHERE key IN ('last_scan','last_report')")
    global _LAST_REPORT
    _LAST_REPORT = {}
    return int(row["n"]) if row else 0


# ── meta ─────────────────────────────────────────────────────────────────────


def meta_get(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):  # pragma: no cover
        return default


def meta_set(key: str, value: Any) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def last_scan() -> float:
    try:
        return float(meta_get("last_scan", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover
        return 0.0


# ── scanning / refresh ───────────────────────────────────────────────────────


def _prune(known: set[str]) -> int:
    removed = 0
    with _connect() as conn:
        rows = conn.execute("SELECT path FROM projects").fetchall()
        for row in rows:
            path = str(row["path"])
            if path in known:
                continue
            try:
                alive = os.path.isdir(path)
            except OSError:  # pragma: no cover
                alive = False
            if not alive:
                conn.execute("DELETE FROM projects WHERE path = ?", (path,))
                removed += 1
    return removed


def ensure_index(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """Walk the configured roots when the cache is cold or older than the TTL."""
    global _LAST_REPORT
    with _LOCK:
        now = time.time()
        existing = count()
        last = last_scan()
        if not force and existing and settings.ttl_seconds > 0 and (now - last) < settings.ttl_seconds:
            return {
                "refreshed": False,
                "reason": "cache fresh",
                "projects": existing,
                "age_seconds": round(now - last, 1),
                "report": _LAST_REPORT or meta_get("last_report", {}) or {},
            }

        report = detect.scan_roots(
            settings.roots,
            max_depth=settings.max_depth,
            max_projects=settings.max_projects,
            seconds=settings.scan_seconds,
        )
        found = report.get("projects") or []
        for project in found:
            save(project)
        removed = _prune({str(project.get("path")) for project in found if project.get("path")})
        summary = {
            "roots": report.get("roots") or [],
            "skipped_roots": report.get("skipped_roots") or [],
            "dirs_visited": report.get("dirs_visited") or 0,
            "truncated": bool(report.get("truncated")),
            "elapsed": report.get("elapsed") or 0,
            "found": len(found),
            "removed": removed,
        }
        meta_set("last_scan", report.get("scanned_at") or time.time())
        meta_set("last_report", summary)
        _LAST_REPORT = summary
        return {
            "refreshed": True,
            "reason": "rescan",
            "projects": count(),
            "found": len(found),
            "removed": removed,
            "age_seconds": 0.0,
            "report": summary,
        }


def refresh_path(settings: Settings, path: str | Path) -> dict[str, Any] | None:
    """Re-detect one directory (or its nearest project ancestor) and store it."""
    try:
        real = os.path.realpath(str(path))
    except OSError:  # pragma: no cover
        return None
    if not os.path.isdir(real):
        return None
    project = detect.detect_dir(Path(real))
    if project is None:
        project = detect.detect_chain(Path(real))
    if project is None:
        return None
    save(project)
    return project


def _stale(project: dict[str, Any], settings: Settings, now: float | None = None) -> bool:
    if settings.ttl_seconds <= 0:
        return True
    stamped = project.get("scanned_at")
    if not isinstance(stamped, (int, float)):
        # Cards do not carry their own timestamp; the row does. Treat unknown as
        # stale so one extra detection happens rather than serving old data.
        return True
    return (now or time.time()) - float(stamped) > settings.ttl_seconds


def resolve(settings: Settings, target: str | Path | None = None) -> dict[str, Any] | None:
    """Find a project by path, path fragment, or name. Never raises."""
    text = str(target or "").strip()
    if not text:
        return resolve_cwd(settings, os.getcwd())

    candidate = expand_path(text)
    if not candidate.is_absolute():
        candidate = Path(os.getcwd()) / candidate
    exists = False
    try:
        exists = candidate.exists()
    except OSError:
        exists = False

    if exists:
        try:
            real = os.path.realpath(str(candidate))
        except OSError:  # pragma: no cover
            real = str(candidate)
        if os.path.isfile(real):
            real = os.path.dirname(real)
        row = get(real)
        if row is not None:
            if _stale(row, settings):
                refreshed = refresh_path(settings, real)
                if refreshed is not None:
                    return refreshed
            return row
        project = detect.detect_chain(Path(real))
        if project is not None:
            save(project)
            return project
        return None

    # Not a path that exists: exact name, then prefix, then substring.
    projects = all_projects("", limit=settings.max_projects)
    lowered = text.lower()
    for matcher in (
        lambda p: str(p.get("name") or "").lower() == lowered,
        lambda p: str(p.get("name") or "").lower().startswith(lowered),
        lambda p: lowered in str(p.get("name") or "").lower(),
        lambda p: lowered in str(p.get("path") or "").lower(),
    ):
        for project in projects:
            if matcher(project):
                return project
    return None


def resolve_cwd(settings: Settings, cwd: str | Path | None) -> dict[str, Any] | None:
    """The project containing *cwd*: indexed row first, then a cheap ancestor probe."""
    if not cwd:
        return None
    try:
        current = Path(os.path.realpath(str(cwd)))
    except OSError:  # pragma: no cover
        return None
    try:
        if current.is_file():
            current = current.parent
    except OSError:  # pragma: no cover
        pass

    # Walk up from cwd looking for an indexed project (the common case: the
    # session started in a subdirectory of a project that was already scanned).
    probe = current
    for _ in range(8):
        row = get(probe)
        if row is not None:
            if _stale(row, settings):
                refreshed = refresh_path(settings, probe)
                if refreshed is not None:
                    return refreshed
            return row
        parent = probe.parent
        if parent == probe:
            break
        probe = parent

    # Nothing indexed: detect the nearest project ancestor from markers. This is
    # one scandir per level, no full root walk, so session start stays instant.
    project = detect.detect_chain(current)
    if project is not None:
        save(project)
    return project


def stats(settings: Settings) -> dict[str, Any]:
    report = _LAST_REPORT or meta_get("last_report", {}) or {}
    scanned = last_scan()
    return {
        "count": count(),
        "last_scan": scanned,
        "age_seconds": round(time.time() - scanned, 1) if scanned else None,
        "report": report,
        "roots": [str(path) for path in settings.roots],
        "db": str(db_path()),
    }

