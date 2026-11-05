"""/atlas — the same index, for a person.

Commands answer in sentences, never JSON, and return text instead of printing: the
handler runs inside the gateway process (see the plugin guide), so ``print`` would
land in a log nobody reads.
"""

from __future__ import annotations

from typing import Any

from . import store, tools
from .settings import Settings

_HELP = (
    "/atlas                    what atlas knows: projects, roots, cache age\n"
    "/atlas list [query]       indexed projects, newest commit first\n"
    "/atlas info <name|path>   full card: stack, commands, branch, layout\n"
    "/atlas commands <name>    just the runnable commands\n"
    "/atlas refresh [name]     re-scan every root, or one project\n"
    "/atlas show               the card that would be injected into this session\n"
    "/atlas forget             drop the index (scan the roots again with /atlas refresh)"
)


def handle(raw_args: str = "", **kwargs: Any) -> str:
    """Entry point for /atlas: never raises, always answers in text."""
    try:
        return _handle(raw_args, **kwargs)
    except Exception as exc:  # pragma: no cover - a command must not break the session
        return (
            f"atlas: the command failed ({type(exc).__name__}: {exc}). "
            f"/atlas refresh rebuilds the index; deleting {store.db_path()} resets it."
        )


def _handle(raw_args: str = "", **kwargs: Any) -> str:
    text = (raw_args or "").strip()
    lowered = text.lower()

    if lowered in {"", "status"}:
        return _status()
    if lowered in {"help", "?", "-h", "--help"}:
        return _HELP
    if lowered in {"list", "ls"}:
        return tools.project_list({"query": ""})
    if lowered.startswith("list ") or lowered.startswith("ls "):
        return tools.project_list({"query": text.split(None, 1)[1]})

    if lowered.startswith("info "):
        return tools.project_info({"path_or_name": text.split(None, 1)[1]})
    if lowered.startswith("show"):
        from . import session_card

        card = session_card({"cwd": ""})
        if not card:
            return (
                "No session card would be injected: the current directory is not inside an indexed "
                "project. /atlas list shows what is known; /atlas refresh scans the roots."
            )
        return card
    if lowered.startswith("commands "):
        return tools.project_commands({"path_or_name": text.split(None, 1)[1]})

    if lowered == "refresh" or lowered == "rescan":
        return tools.project_refresh({})
    if lowered.startswith("refresh ") or lowered.startswith("rescan "):
        return tools.project_refresh({"path_or_name": text.split(None, 1)[1]})

    if lowered in {"forget", "clear"}:
        removed = store.clear()
        return (
            f"atlas index cleared ({removed} projects). The projects themselves are untouched — "
            "/atlas refresh rebuilds the index."
        )

    # Bare words are a search: "/atlas billing" == "/atlas list billing".
    return tools.project_list({"query": text})


def _status() -> str:
    settings: Settings = tools.get_settings()
    data = store.stats(settings)
    lines = [
        f"atlas — {data['count']} projects indexed"
        + (f", last scan {tools._age(data.get('age_seconds'))}" if data.get("age_seconds") is not None else ", never scanned"),
        settings.describe(),
        f"index: {data['db']}",
    ]
    report = data.get("report") or {}
    if report.get("truncated"):
        lines.append("the last scan hit its budget and was partial — raise scan_seconds or narrow roots")
    for skipped in report.get("skipped_roots") or []:
        lines.append(f"skipped root {skipped.get('root')}: {skipped.get('why')}")
    known = store.all_projects("", limit=5)
    if known:
        lines.append("newest: " + ", ".join(str(project.get("name")) for project in known))
    lines.append("")
    lines.append("/atlas list · /atlas info <name> · /atlas refresh")
    return "\n".join(lines)
