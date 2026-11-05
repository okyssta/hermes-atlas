"""Settings resolution for the atlas plugin.

Every key here mirrors a ``config_schema`` entry in ``plugin.yaml`` one-for-one:
the Desktop settings form, ``hermes config set`` and this module must agree on a
single source of truth (``tests/test_manifest.py`` asserts the key sets match).
Values are coerced defensively — a typo in ``config.yaml`` must never stop the
plugin from answering.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_ID = "atlas"

SCHEMA_KEYS = (
    "enabled",
    "roots",
    "max_depth",
    "max_projects",
    "scan_seconds",
    "cache_ttl_minutes",
    "inject_prompt_section",
)

# Where people actually keep code. Only the ones that exist are used; the
# session's own cwd is always included, so a plugin installed from inside a
# project still finds that project without configuration.
DEFAULT_ROOT_HINTS = ("~/code", "~/projects", "~/src", "~/work", "~/repos")

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_PROJECTS = 500
DEFAULT_SCAN_SECONDS = 6.0
DEFAULT_TTL_MINUTES = 60


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def expand_path(raw: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and ``$VARS`` without touching the filesystem."""
    return Path(os.path.expandvars(os.path.expanduser(str(raw).strip())))


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = os.path.realpath(str(path))
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def default_roots(cwd: str | Path | None = None) -> list[Path]:
    """The current directory plus whichever conventional code folders exist."""
    candidates: list[Path] = []
    base = Path(cwd) if cwd else Path.cwd()
    try:
        if base.is_dir():
            candidates.append(base)
    except OSError:
        pass
    for hint in DEFAULT_ROOT_HINTS:
        path = expand_path(hint)
        try:
            if path.is_dir():
                candidates.append(path)
        except OSError:
            continue
    return _dedupe(candidates)


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    roots: tuple[Path, ...] = ()
    max_depth: int = DEFAULT_MAX_DEPTH
    max_projects: int = DEFAULT_MAX_PROJECTS
    scan_seconds: float = DEFAULT_SCAN_SECONDS
    cache_ttl_minutes: int = DEFAULT_TTL_MINUTES
    inject_prompt_section: bool = True

    @property
    def ttl_seconds(self) -> float:
        return float(self.cache_ttl_minutes) * 60.0

    @classmethod
    def from_ctx(cls, ctx: Any, cwd: str | Path | None = None) -> "Settings":
        get = getattr(ctx, "get_config", None)
        if not callable(get):  # pragma: no cover - older hosts / stubs
            return cls(roots=tuple(default_roots(cwd)))

        def value(key: str, fallback: Any) -> Any:
            try:
                resolved = get(key, default=fallback)
            except TypeError:  # hosts whose get_config takes one argument
                resolved = get(key)
            return fallback if resolved is None else resolved

        raw_roots = value("roots", [])
        if isinstance(raw_roots, str):
            raw_roots = [part for part in raw_roots.replace(",", "\n").splitlines()]
        roots: list[Path] = []
        for entry in raw_roots or ():
            text = str(entry).strip()
            if not text:
                continue
            path = expand_path(text)
            try:
                # A configured root that does not exist is reported by the scan
                # rather than silently dropped, so the user can fix the typo.
                roots.append(path)
            except OSError:  # pragma: no cover - expand_path never stats
                continue
        roots = _dedupe(roots)
        if not roots:
            roots = default_roots(cwd)

        return cls(
            enabled=_bool(value("enabled", True), True),
            roots=tuple(roots),
            max_depth=_int(value("max_depth", DEFAULT_MAX_DEPTH), DEFAULT_MAX_DEPTH, 1, 8),
            max_projects=_int(value("max_projects", DEFAULT_MAX_PROJECTS), DEFAULT_MAX_PROJECTS, 1, 5000),
            scan_seconds=_float(value("scan_seconds", DEFAULT_SCAN_SECONDS), DEFAULT_SCAN_SECONDS, 0.5, 60.0),
            cache_ttl_minutes=_int(value("cache_ttl_minutes", DEFAULT_TTL_MINUTES), DEFAULT_TTL_MINUTES, 0, 10080),
            inject_prompt_section=_bool(value("inject_prompt_section", True), True),
        )

    def describe(self) -> str:
        roots = ", ".join(str(path) for path in self.roots) or "(none)"
        return "\n".join(
            [
                f"roots: {roots}",
                f"depth limit: {self.max_depth} · project cap: {self.max_projects}",
                f"scan budget: {self.scan_seconds:g}s · cache TTL: {self.cache_ttl_minutes} min",
                f"session card: {'on' if self.inject_prompt_section else 'off'}",
            ]
        )
