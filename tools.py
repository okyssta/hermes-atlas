"""Tool handlers and the text they return.

Contract: ``def handler(args: dict, **kwargs) -> str``, never raises, always
returns text a model can act on. The cards are plain text on purpose — they end
up in a transcript, and the commands in them are meant to be copied into a
terminal, so no JSON quoting sits between the model and the command.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import store
from .settings import Settings

MAX_LIST = 40
PROMPT_CARD_LIMIT = 900
_COMMAND_DISPLAY_LIMIT = 140


def get_settings() -> Settings:
    from . import current_settings

    return current_settings()


# ── formatting ───────────────────────────────────────────────────────────────


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _commit_day(project: dict[str, Any]) -> str:
    stamp = project.get("last_commit") or ""
    return str(stamp)[:10] if stamp else "no commits"


def format_list(projects: list[dict[str, Any]], stats: dict[str, Any], query: str = "") -> str:
    if not projects:
        lines = [f"Atlas knows no projects{' matching ' + repr(query) if query else ''}."]
        info = stats.get("report") or {}
        if stats.get("roots"):
            lines.append(f"roots: {', '.join(stats['roots'])}")
        if info.get("truncated"):
            lines.append("the last scan hit its time/project budget and was partial; raise scan_seconds or narrow roots")
        lines.append("Call project_refresh to scan again, or project_refresh <path> to index one directory.")
        return "\n".join(lines)

    header = f"Atlas — {len(projects)} project{'s' if len(projects) != 1 else ''}"
    if query:
        header += f" matching {query!r}"
    scanned = stats.get("age_seconds")
    if scanned is not None:
        header += f" · last scan {_age(scanned)}"
    lines = [header]
    for project in projects:
        label = _stack_badge(project)
        lines.append(
            "  {name:<24} {stack:<20} {path}  ({when})".format(
                name=str(project.get("name") or "?")[:24],
                stack=label[:20],
                path=str(project.get("path") or ""),
                when=_commit_day(project),
            )
        )
    total = stats.get("count") or len(projects)
    lines.append("")
    lines.append(
        f"showing {len(projects)} of {total} · newest commit first · "
        "project_info <name|path> for a full card, project_commands for just the commands"
    )
    return "\n".join(line.rstrip() for line in lines)


def _stack_badge(project: dict[str, Any]) -> str:
    """Primary stack for the list column, plus how many other stacks follow."""
    label = str(project.get("stack_label") or project.get("stack") or "?")
    parts = [part for part in label.split(" + ") if part]
    if len(parts) > 1:
        return f"{parts[0]} +{len(parts) - 1}"
    return label


def format_card(project: dict[str, Any]) -> str:
    name = project.get("name") or project.get("path") or "?"
    label = project.get("stack_label") or project.get("stack") or "unknown"
    lines = [f"{name} — {label}", f"  path      {project.get('path', '')}"]

    manager = project.get("package_manager") or ""
    if manager:
        reason = project.get("package_manager_reason") or ""
        lines.append(f"  manager   {manager}{f' ({reason})' if reason else ''}")

    markers = project.get("markers") or []
    if markers:
        lines.append(f"  markers   {', '.join(markers[:8])}")

    branch = project.get("default_branch") or project.get("branch") or ""
    commit = project.get("last_commit") or ""
    if branch or commit:
        when = str(commit).replace("T", " ").replace("Z", " UTC") if commit else "no commit log"
        lines.append(f"  branch    {branch or '?'} · last commit {when}")

    readme = project.get("readme_title") or ""
    if readme:
        lines.append(f"  readme    {readme}")

    env_files = project.get("env_files") or []
    if env_files:
        lines.append(f"  env files {', '.join(env_files)} (names only — contents never read)")

    ports = project.get("ports") or []
    if ports:
        rendered = ", ".join(f"{item.get('port')} ({item.get('source')})" for item in ports[:6])
        lines.append(f"  ports     {rendered}")

    services = project.get("services") or []
    if services:
        rendered = ", ".join(
            f"{service.get('name')}" + (f":{','.join(str(p) for p in service.get('ports') or [])}" if service.get("ports") else "")
            for service in services[:8]
        )
        lines.append(f"  services  {rendered}")

    layout = project.get("layout") or []
    if layout:
        shown = ", ".join(layout[:8])
        extra = len(project.get("layout") or []) - 8
        lines.append(f"  layout    {shown}{f' (+{extra} more)' if extra > 0 else ''}")

    commands = project.get("commands") or []
    if commands:
        lines.append("  commands")
        for command in commands:
            task = str(command.get("task") or "")[:9]
            text = str(command.get("command") or "")[:_COMMAND_DISPLAY_LIMIT]
            reason = str(command.get("reason") or "")
            lines.append(f"    {task:<9} {text}" + (f"   # {reason}" if reason else ""))
    else:
        lines.append("  commands  none detected from the marker files present")

    notes = project.get("notes") or []
    if notes:
        lines.append("  notes")
        for note in notes[:6]:
            lines.append(f"    - {note}")
    return "\n".join(lines)


def format_commands(project: dict[str, Any]) -> str:
    name = project.get("name") or "?"
    path = project.get("path") or ""
    commands = project.get("commands") or []
    lines = [f"# {name} — {path}"]
    if not commands:
        lines.append("# no commands could be derived from the marker files in this project")
        return "\n".join(lines)
    for command in commands:
        lines.append(str(command.get("command") or ""))
    return "\n".join(lines)


def prompt_card(project: dict[str, Any], limit: int = PROMPT_CARD_LIMIT) -> str:
    """The bounded session card: only what stops a wrong first command."""
    name = project.get("name") or "?"
    label = project.get("stack_label") or project.get("stack") or "unknown"
    path = str(project.get("path") or "")
    lines = [f"Atlas: project {name} · {label}", f"path: {path}"]
    seen_tasks: set[str] = set()
    for command in project.get("commands") or []:
        task = str(command.get("task") or "").strip()
        text = str(command.get("command") or "").strip()
        if not task or not text or task in seen_tasks:
            continue
        seen_tasks.add(task)
        if len(text) > 120:
            text = text[:119] + "…"
        lines.append(f"{task}: {text}")
    extras: list[str] = []
    env_files = project.get("env_files") or []
    if env_files:
        extras.append(f"env files: {', '.join(env_files[:4])}")
    branch = project.get("default_branch") or ""
    if branch:
        extras.append(f"branch: {branch}")
    if extras:
        lines.append(" · ".join(extras))
    lines.append("Use these exact commands instead of guessing. Call project_info for the full card.")
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    # Drop command lines from the bottom until it fits; the header, the path and
    # the instruction line are what make the card useful, so they stay.
    tail = lines[-1]
    kept = lines[:2]
    budget = limit - len("\n".join(kept + [tail])) - 2
    for line in lines[2:-1]:
        if len(line) + 1 > budget:
            break
        kept.append(line)
        budget -= len(line) + 1
    kept.append(tail)
    text = "\n".join(kept)
    return text[:limit]


# ── handlers ─────────────────────────────────────────────────────────────────


def _error(what: str, exc: Exception) -> str:
    return f"Error: atlas could not {what} ({type(exc).__name__}: {exc}). The index is at {store.db_path()}."


def project_list(args: dict | None = None, **kwargs: Any) -> str:
    data = args or {}
    query = str(data.get("query") or "").strip()
    try:
        settings = get_settings()
        if not settings.enabled:
            return "Atlas is disabled (config: enabled=false). No scan was run."
        store.ensure_index(settings)
        everything = store.all_projects(query, limit=settings.max_projects)
        stats_data = store.stats(settings)
        stats_data["count"] = stats_data.get("count") or len(everything)
        return format_list(everything[:MAX_LIST], stats_data, query=query)
    except Exception as exc:  # pragma: no cover - defensive contract
        return _error("list projects", exc)


def project_info(args: dict | None = None, **kwargs: Any) -> str:
    data = args or {}
    target = str(data.get("path_or_name") or "").strip()
    try:
        settings = get_settings()
        if not settings.enabled:
            return "Atlas is disabled (config: enabled=false). No card is available."
        project = store.resolve(settings, target)
        if project is None:
            # A name lookup on a cold index: build it once, then ask again. This
            # is TTL-guarded, so a warm index pays nothing.
            store.ensure_index(settings)
            project = store.resolve(settings, target)
        if project is None:
            return _unknown(settings, target)
        return format_card(project)
    except Exception as exc:  # pragma: no cover - defensive contract
        return _error("read that project", exc)


def project_commands(args: dict | None = None, **kwargs: Any) -> str:
    data = args or {}
    target = str(data.get("path_or_name") or "").strip()
    try:
        settings = get_settings()
        if not settings.enabled:
            return "Atlas is disabled (config: enabled=false). No commands are available."
        project = store.resolve(settings, target)
        if project is None:
            store.ensure_index(settings)
            project = store.resolve(settings, target)
        if project is None:
            return _unknown(settings, target) + "\n\nNothing to run: no project was found."
        return format_commands(project)
    except Exception as exc:  # pragma: no cover - defensive contract
        return _error("derive those commands", exc)


def project_refresh(args: dict | None = None, **kwargs: Any) -> str:
    data = args or {}
    target = str(data.get("path_or_name") or "").strip()
    try:
        settings = get_settings()
        if not settings.enabled:
            return "Atlas is disabled (config: enabled=false). Nothing was scanned."
        if target:
            return _refresh_one(settings, target)
        result = store.ensure_index(settings, force=True)
        report = result.get("report") or {}
        lines = [
            f"Atlas rescan — {result.get('projects', 0)} projects indexed "
            f"({result.get('found', 0)} found in this pass, {result.get('removed', 0)} pruned)",
            f"roots: {', '.join(report.get('roots') or []) or '(none)'}",
            f"visited {report.get('dirs_visited', 0)} directories in {report.get('elapsed', 0)}s",
        ]
        if report.get("truncated"):
            lines.append(
                "partial scan: the time or project budget was reached, so some projects were not visited "
                "(raise scan_seconds/max_projects, or pass a narrower roots list)"
            )
        for skipped in report.get("skipped_roots") or []:
            lines.append(f"skipped root {skipped.get('root')}: {skipped.get('why')}")
        projects = store.all_projects("", limit=settings.max_projects)
        if projects:
            lines.append("")
            lines.append(format_list(projects[:MAX_LIST], store.stats(settings)))
        return "\n".join(lines)
    except Exception as exc:  # pragma: no cover - defensive contract
        return _error("refresh the index", exc)


def _refresh_one(settings: Settings, target: str) -> str:
    project = store.resolve(settings, target)
    if project is None:
        return _unknown(settings, target)
    path = str(project.get("path") or "")
    refreshed = store.refresh_path(settings, path)
    if refreshed is None:
        return f"{path} no longer looks like a project (its marker files are gone); the old card is unchanged."
    return "rescanned.\n\n" + format_card(refreshed)


def _unknown(settings: Settings, target: str) -> str:
    known = store.all_projects("", limit=8)
    lines = [
        f"No project in the atlas index matches {target!r}." if target else "No project matches the current directory.",
    ]
    if settings.roots:
        lines.append(f"roots: {', '.join(str(path) for path in settings.roots)}")
    if known:
        lines.append("known projects: " + ", ".join(str(project.get("name")) for project in known))
    lines.append("Call project_refresh <path> to index a directory that is outside the configured roots.")
    return "\n".join(lines)


def resolve_for_cwd(cwd: str | Path | None) -> dict[str, Any] | None:
    """Used by the system-prompt section; never walks the roots."""
    try:
        settings = get_settings()
        if not settings.enabled or not settings.inject_prompt_section:
            return None
        return store.resolve_cwd(settings, cwd or os.getcwd())
    except Exception:  # pragma: no cover - a prompt section must never break a session
        return None
