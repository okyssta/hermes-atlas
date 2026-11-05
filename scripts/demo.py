#!/usr/bin/env python3
"""Demonstration: scan /root read-only, then print two real project cards.

Everything here is the shipping code path — ``detect.scan_roots`` walks the
directories, ``store`` writes only into a throwaway ``HERMES_HOME``, and the
cards are rendered by the same ``tools.format_card`` / ``tools.prompt_card`` the
agent gets.

    /root/peripheral/.venv/bin/python scripts/demo.py

Nothing in /root is written to or executed; the scan reads marker files and
directory names.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))

# A throwaway profile: the demo must not touch the user's real atlas index.
# (Set ATLAS_DEMO_REAL_HOME=1 to deliberately write into the caller's profile.)
if not os.environ.get("ATLAS_DEMO_REAL_HOME"):
    os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="atlas-demo-")

from atlas import detect, store, tools  # noqa: E402
from atlas.settings import Settings  # noqa: E402

ROOT = Path("/root")
TARGETS = (Path("/root/peripheral"), Path("/root/.hermes/hermes-agent"))


def rule(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def main() -> int:
    print(f"atlas demo · HERMES_HOME={os.environ['HERMES_HOME']} (throwaway)")
    print(f"scanning {ROOT} — read-only, no project code executed")

    settings = Settings(
        enabled=True,
        roots=(ROOT,),
        max_depth=3,
        max_projects=500,
        scan_seconds=20.0,
        cache_ttl_minutes=60,
        inject_prompt_section=True,
    )

    result = store.ensure_index(settings)
    report = result["report"]
    rule("scan report")
    print(f"roots:            {', '.join(report.get('roots') or [])}")
    print(f"directories:      {report.get('dirs_visited')}")
    print(f"projects found:   {report.get('found')} (index holds {result.get('projects')})")
    print(f"wall clock:       {report.get('elapsed')}s")
    print(f"budget reached:   {report.get('truncated')}  (time/project cap; the walk is bounded)")
    for skipped in report.get("skipped_roots") or []:
        print(f"skipped root:     {skipped.get('root')} — {skipped.get('why')}")

    for target in TARGETS:
        rule(f"project_info — {target}")
        card = store.get(target) or detect.detect_dir(target)
        if card is None:
            print("not a project")
            continue
        if store.get(target) is None:
            print("(below the scan's depth/budget — detected directly for this demo)")
        print(tools.format_card(card))

    rule("system card injected when cwd is inside /root/peripheral")
    card = store.get(TARGETS[0]) or detect.detect_dir(TARGETS[0])
    injected = tools.prompt_card(card, limit=900)
    print(injected)
    print(f"\n[{len(injected)} chars, cap 900 · nothing is injected when cwd is outside every known project]")

    rule("project_list over the scan")
    projects = store.all_projects("", limit=40)
    print(tools.format_list(projects[:12], store.stats(settings), query=""))

    print()
    print(f"index written to {store.db_path()} (inside the throwaway HERMES_HOME)")
    print("scanned projects were only read: no files written, no commands run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
