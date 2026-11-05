"""atlas — the agent stops guessing how your projects are built.

Hermes can read a repository, but it does not know that *this* checkout is a uv
project whose tests run through ``uv run pytest``, or that the other one is a Go
module with no ``Makefile`` at all. So it guesses ``npm test`` in a Python repo,
invents a dev-server port, or asks a question the filesystem already answers.

atlas scans the folders people keep code in — once, read-only — detects each
project's stack from its marker files, derives the exact install/test/lint/build/
run invocations, and caches one card per project in SQLite under the profile's
``plugin-data/atlas/``. At the start of every session whose working directory is
inside a known project it injects a bounded card into the system prompt (name,
stack, exact commands), so the first command the agent runs is the right one.

What it adds:

* **``project_list`` / ``project_info`` / ``project_commands`` / ``project_refresh``**
  — find projects, read one card (each command carries the marker that justifies
  it), copy just the commands, or re-scan.
* **A system-prompt section** — a ≤900-character card, only when the cwd is
  inside a known project.
* **``/atlas``** — the same surfaces for a person: ``list``, ``info <name>``,
  ``refresh``.

What it refuses to do: execute anything in the scanned projects, write into
them, or read the contents of ``.env`` files (names are listed, values never
are). Detection is marker-file based; when no marker declares a command, the card
says so instead of inventing one.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Mapping

from . import commands, schemas, store, tools
from .settings import PLUGIN_ID, SCHEMA_KEYS, Settings

logger = logging.getLogger(__name__)

_CTX: Any = None
_CACHED: Settings | None = None
_CACHED_AT = 0.0
_CACHE_SECONDS = 2.0
_SECTION_CACHE: dict[tuple[str, int], str] = {}

PROMPT_SECTION_ID = "atlas.project"
PROMPT_SECTION_POSITION = "after_memory"
PROMPT_SECTION_MAX_CHARS = 900


def _hermes_home() -> Path | None:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - older hosts
        return None


def _session_cwd() -> str:
    ctx = _CTX
    if ctx is not None:
        try:
            cwd = getattr(ctx, "session_cwd", "")
            if isinstance(cwd, str) and cwd:
                return cwd
        except Exception:  # pragma: no cover - property may raise in odd hosts
            pass
    try:
        import os

        return os.getcwd()
    except Exception:  # pragma: no cover
        return ""


def current_settings() -> Settings:
    """Resolved settings, memoised briefly: a tool loop calls this repeatedly."""
    global _CACHED, _CACHED_AT

    now = time.monotonic()
    if _CACHED is not None and now - _CACHED_AT < _CACHE_SECONDS:
        return _CACHED
    if _CTX is None:
        _CACHED = Settings.from_ctx(None, cwd=_session_cwd())
    else:
        _CACHED = Settings.from_ctx(_CTX, cwd=_session_cwd())
    _CACHED_AT = now
    return _CACHED


def reset_caches() -> None:
    """Drop the settings + session-card caches (used by /atlas refresh and tests)."""
    global _CACHED, _CACHED_AT
    _CACHED = None
    _CACHED_AT = 0.0
    _SECTION_CACHE.clear()


# ── system prompt section ────────────────────────────────────────────────────


def session_card(info: Mapping[str, Any] | None) -> str:
    """Render the bounded project card for a session, or ``""`` outside a project."""
    try:
        cwd = ""
        if isinstance(info, Mapping):
            cwd = str(info.get("cwd") or "")
        if not cwd:
            cwd = _session_cwd()
        if not cwd:
            return ""
        project = tools.resolve_for_cwd(cwd)
        if not project:
            return ""
        key = (str(project.get("path") or ""), int(float(project.get("scanned_at") or 0)))
        cached = _SECTION_CACHE.get(key)
        if cached is not None:
            return cached
        text = tools.prompt_card(project, limit=PROMPT_SECTION_MAX_CHARS)
        if len(_SECTION_CACHE) >= 32:
            _SECTION_CACHE.clear()
        _SECTION_CACHE[key] = text
        logger.debug("atlas: session card for %s (%d chars)", project.get("path"), len(text))
        return text
    except Exception:  # pragma: no cover - a prompt section must never break a turn
        logger.debug("atlas: could not render the session card", exc_info=True)
        return ""


# ── register ─────────────────────────────────────────────────────────────────


def _safe(label: str, function: Any) -> None:
    try:
        function()
    except Exception:  # pragma: no cover - one broken surface must not disable the rest
        logger.warning("atlas: could not register %s", label, exc_info=True)


def register(ctx: Any) -> None:
    global _CTX

    _CTX = ctx
    reset_caches()
    settings = current_settings()

    handlers = {
        "project_list": tools.project_list,
        "project_info": tools.project_info,
        "project_commands": tools.project_commands,
        "project_refresh": tools.project_refresh,
    }
    for schema in schemas.ALL:
        _safe(
            f"tool {schema['name']}",
            lambda schema=schema: ctx.register_tool(
                name=schema["name"],
                toolset="atlas",
                schema=schema,
                handler=handlers[schema["name"]],
            ),
        )

    _safe(
        "command /atlas",
        lambda: ctx.register_command(
            "atlas",
            commands.handle,
            description="Local project registry: exact commands per project",
            args_hint="[list|info <name>|refresh [name]]",
        ),
    )

    if hasattr(ctx, "register_system_prompt_section"):
        _safe(
            "system prompt section",
            lambda: ctx.register_system_prompt_section(
                PROMPT_SECTION_ID,
                session_card,
                position=PROMPT_SECTION_POSITION,
                max_chars=PROMPT_SECTION_MAX_CHARS,
            ),
        )

    try:
        indexed = store.count()
    except Exception:  # pragma: no cover - a read-only home must not block loading
        indexed = "?"
    logger.info(
        "atlas ready: %s roots, %s indexed, session card %s",
        len(settings.roots),
        indexed,
        "on" if settings.inject_prompt_section else "off",
    )


__all__ = [
    "register",
    "current_settings",
    "reset_caches",
    "session_card",
    "PLUGIN_ID",
    "SCHEMA_KEYS",
    "store",
]
