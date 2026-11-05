"""The index: realpath keys, TTL refresh, cwd resolution, bounded scans."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import make_project, package_json

from atlas import detect, store


def _node_project(root: Path, name: str = "app") -> Path:
    return make_project(root, name, {"package.json": package_json(scripts={"test": "jest"})})


def test_scan_indexes_by_realpath_and_resolves_nested_cwd(tmp_path, settings_factory):
    root = tmp_path / "code"
    project = _node_project(root, "app")
    deep = project / "src" / "components"
    deep.mkdir(parents=True)
    settings = settings_factory([root])

    result = store.ensure_index(settings)
    assert result["refreshed"] is True
    assert result["found"] == 1
    assert store.count() == 1

    card = store.resolve_cwd(settings, deep)
    assert card is not None
    assert card["path"] == str(project.resolve())
    assert card["name"] == "app"
    # The row is keyed by realpath, so a symlinked cwd resolves to the same row.
    link = tmp_path / "linked"
    os.symlink(project, link)
    assert store.resolve_cwd(settings, link)["path"] == str(project.resolve())


def test_resolve_by_name_path_and_unknown(tmp_path, settings_factory):
    root = tmp_path / "code"
    alpha = _node_project(root, "alpha-service")
    _node_project(root, "beta")
    settings = settings_factory([root])
    store.ensure_index(settings)

    assert store.resolve(settings, "beta")["name"] == "beta"
    assert store.resolve(settings, "alpha")["name"] == "alpha-service"  # prefix
    assert store.resolve(settings, str(alpha))["name"] == "alpha-service"
    (alpha / "src").mkdir()
    assert store.resolve(settings, str(alpha / "src"))["name"] == "alpha-service"
    assert store.resolve(settings, "nope-not-here") is None


def test_ttl_serves_the_cache_then_refresh_picks_up_a_new_project(tmp_path, settings_factory):
    root = tmp_path / "code"
    _node_project(root, "one")
    settings = settings_factory([root])
    first = store.ensure_index(settings)
    assert first["refreshed"] is True and first["projects"] == 1

    _node_project(root, "two")
    cached = store.ensure_index(settings)
    assert cached["refreshed"] is False
    assert cached["reason"] == "cache fresh"
    assert store.count() == 1

    forced = store.ensure_index(settings, force=True)
    assert forced["refreshed"] is True
    assert forced["found"] == 2
    assert store.count() == 2


def test_zero_ttl_rescans_on_every_lookup(tmp_path, settings_factory):
    root = tmp_path / "code"
    _node_project(root, "one")
    settings = settings_factory([root], cache_ttl_minutes=0)
    assert store.ensure_index(settings)["refreshed"] is True
    assert store.ensure_index(settings)["refreshed"] is True


def test_refresh_one_project_updates_its_card(tmp_path, settings_factory):
    root = tmp_path / "code"
    project = make_project(root, "app", {"pyproject.toml": "[project]\nname = \"app\"\n"})
    settings = settings_factory([root])
    store.ensure_index(settings)
    assert store.get(project)["package_manager"] == "pip"

    (project / "uv.lock").write_text("version = 1\n")
    refreshed = store.refresh_path(settings, project)
    assert refreshed["package_manager"] == "uv"
    assert store.get(project)["package_manager"] == "uv"
    assert store.refresh_path(settings, tmp_path / "missing") is None


def test_scan_respects_the_depth_limit(tmp_path):
    root = tmp_path / "deep"
    project = make_project(root, "l1/l2/proj", {"package.json": package_json(scripts={"test": "x"})})
    shallow = detect.scan_roots([root], max_depth=2, max_projects=20, seconds=5.0)
    assert shallow["projects"] == []
    deep = detect.scan_roots([root], max_depth=3, max_projects=20, seconds=5.0)
    assert [item["path"] for item in deep["projects"]] == [str(project.resolve())]


def test_symlink_loops_do_not_hang_the_scan(tmp_path):
    root = tmp_path / "loop"
    project = _node_project(root, "app")
    os.symlink(root, project / "back-to-root")
    os.symlink(project, project / "self")
    report = detect.scan_roots([root], max_depth=5, max_projects=20, seconds=5.0)
    assert [item["name"] for item in report["projects"]] == ["app"]
    assert report["truncated"] is False
    assert report["elapsed"] < 5.0


def test_project_cap_is_enforced_and_reported(tmp_path):
    root = tmp_path / "many"
    for index in range(5):
        _node_project(root, f"proj{index}")
    report = detect.scan_roots([root], max_depth=3, max_projects=2, seconds=10.0)
    assert len(report["projects"]) == 2
    assert report["truncated"] is True


def test_missing_roots_are_reported_not_fatal(tmp_path, settings_factory):
    settings = settings_factory([tmp_path / "does-not-exist", tmp_path / "code"])
    make_project(tmp_path / "code", "app", {"package.json": package_json(scripts={"test": "x"})})
    result = store.ensure_index(settings)
    report = result["report"]
    assert result["found"] == 1
    assert report["skipped_roots"][0]["why"] == "not a directory"
    assert report["roots"], report


def test_pruning_drops_projects_that_disappeared(tmp_path, settings_factory):
    root = tmp_path / "code"
    gone = _node_project(root, "gone")
    _node_project(root, "stays")
    settings = settings_factory([root])
    store.ensure_index(settings)
    assert store.count() == 2

    import shutil

    shutil.rmtree(gone)
    result = store.ensure_index(settings, force=True)
    assert result["removed"] == 1
    assert store.count() == 1


def test_clear_empties_the_index_without_touching_projects(tmp_path, settings_factory):
    root = tmp_path / "code"
    project = _node_project(root, "app")
    settings = settings_factory([root])
    store.ensure_index(settings)
    assert store.clear() == 1
    assert store.count() == 0
    assert (project / "package.json").is_file()


def test_resolve_accepts_a_file_inside_the_project(tmp_path, settings_factory):
    root = tmp_path / "code"
    project = _node_project(root, "app")
    settings = settings_factory([root])
    store.ensure_index(settings)
    assert store.resolve(settings, project / "package.json")["name"] == "app"
