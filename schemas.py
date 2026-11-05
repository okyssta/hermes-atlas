"""Tool schemas.

Descriptions target a model that has never heard of this plugin: they say what
the index contains, when reaching for it beats guessing a command, and that the
commands come from marker files rather than convention.
"""

from __future__ import annotations

PROJECT_LIST = {
    "name": "project_list",
    "description": (
        "List the local projects atlas has indexed: name, stack/package manager, path and last commit, "
        "newest first. Use it before assuming how a repository is built, or when the user says 'this "
        "project' / 'my other repo' and you need to know what is on the machine. An empty query lists "
        "everything (capped); a query filters by name, path or stack. Detection is read-only: no project "
        "code is ever executed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional filter, e.g. 'python', 'rust', 'billing'. Empty lists everything.",
            }
        },
        "required": [],
    },
}

PROJECT_INFO = {
    "name": "project_info",
    "description": (
        "The full card for one project: stack, package manager, install/test/lint/build/run commands each "
        "with the marker file that justifies it, env file NAMES (values are never read), git branch and "
        "last commit, README title and a top-level layout. Use it before running anything in an unfamiliar "
        "repository, or when the user asks how a project is set up. Accepts a project name or a path; an "
        "empty value means the current directory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path_or_name": {
                "type": "string",
                "description": "Project name ('peripheral'), absolute/relative path, or empty for the cwd.",
            }
        },
        "required": [],
    },
}

PROJECT_COMMANDS = {
    "name": "project_commands",
    "description": (
        "Just the runnable commands for one project, one per line and copy-pasteable, with a leading "
        "comment naming the project and its path. Use it when you are about to install, test, lint, build "
        "or run a project and want the exact invocation without the rest of the card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path_or_name": {
                "type": "string",
                "description": "Project name or path; empty means the current directory.",
            }
        },
        "required": [],
    },
}

PROJECT_REFRESH = {
    "name": "project_refresh",
    "description": (
        "Re-scan the configured project roots (or one project) and update the atlas index. Use it after "
        "adding a manifest, after creating a project, or when a card looks stale. Still read-only: files "
        "are read, never written, and no build command is executed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path_or_name": {
                "type": "string",
                "description": "One project to re-detect (name or path). Empty re-scans every root.",
            }
        },
        "required": [],
    },
}

ALL = (PROJECT_LIST, PROJECT_INFO, PROJECT_COMMANDS, PROJECT_REFRESH)
