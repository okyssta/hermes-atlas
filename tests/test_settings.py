"""Settings: defensive coercion, default roots, and the schema/manifest handshake."""

from __future__ import annotations

from pathlib import Path

from conftest import RecordingContext, make_project

from atlas.settings import SCHEMA_KEYS, Settings


def test_defaults_are_sane():
    settings = Settings()
    assert settings.enabled is True
    assert settings.max_depth == 3
    assert settings.max_projects == 500
    assert settings.scan_seconds == 6.0
    assert settings.cache_ttl_minutes == 60
    assert settings.inject_prompt_section is True


def test_config_values_are_clamped_not_trusted():
    ctx = RecordingContext(
        {
            "enabled": "no",
            "max_depth": 99,
            "max_projects": 10**9,
            "scan_seconds": -4,
            "cache_ttl_minutes": "abc",
            "inject_prompt_section": "off",
        }
    )
    settings = Settings.from_ctx(ctx, cwd="/tmp")
    assert settings.enabled is False
    assert settings.max_depth == 8
    assert settings.max_projects == 5000
    assert settings.scan_seconds == 0.5
    assert settings.cache_ttl_minutes == 60
    assert settings.inject_prompt_section is False


def test_roots_expand_home_and_accept_a_comma_string():
    ctx = RecordingContext({"roots": "~/code, ~/work"})
    settings = Settings.from_ctx(ctx, cwd="/tmp")
    assert [str(path) for path in settings.roots] == [
        str(Path.home() / "code"),
        str(Path.home() / "work"),
    ]


def test_default_roots_include_the_cwd(tmp_path):
    project = make_project(tmp_path, "here", {"package.json": "{}\n"})
    settings = Settings.from_ctx(None, cwd=project)
    assert project in [Path(path) for path in settings.roots]
    assert len(settings.roots) == len({str(path) for path in settings.roots})


def test_default_roots_skip_folders_that_do_not_exist(tmp_path):
    settings = Settings.from_ctx(None, cwd=tmp_path)
    assert all(Path(path).is_dir() for path in settings.roots)
    assert tmp_path in [Path(path) for path in settings.roots]


def test_a_get_config_that_takes_one_argument_is_tolerated():
    class Narrow:
        def get_config(self, key):
            return 3 if key == "max_depth" else None

    settings = Settings.from_ctx(Narrow(), cwd="/tmp")
    assert settings.max_depth == 3
    assert settings.enabled is True


def test_schema_keys_are_the_documented_ones():
    assert SCHEMA_KEYS == (
        "enabled",
        "roots",
        "max_depth",
        "max_projects",
        "scan_seconds",
        "cache_ttl_minutes",
        "inject_prompt_section",
    )


def test_roots_accept_a_list_from_config():
    settings = Settings.from_ctx(RecordingContext({"roots": ["/a", "/b"]}), cwd="/tmp")
    assert [str(path) for path in settings.roots] == ["/a", "/b"]
