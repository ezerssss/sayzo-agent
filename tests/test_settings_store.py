"""Tests for the persistent user-settings store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sayzo_agent import settings_store


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert settings_store.load(tmp_path) == {}


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    settings_store.save(tmp_path, {"arm": {"hotkey": "ctrl+alt+x"}})
    loaded = settings_store.load(tmp_path)
    assert loaded == {"arm": {"hotkey": "ctrl+alt+x"}}


def test_save_merges_nested_without_clobbering(tmp_path: Path) -> None:
    settings_store.save(tmp_path, {"arm": {"hotkey": "ctrl+alt+x"}})
    settings_store.save(tmp_path, {"arm": {"poll_interval_secs": 3.5}})
    loaded = settings_store.load(tmp_path)
    assert loaded == {
        "arm": {"hotkey": "ctrl+alt+x", "poll_interval_secs": 3.5}
    }


def test_save_preserves_unknown_top_level_keys(tmp_path: Path) -> None:
    """Forward-compat: a newer build could add keys an older build doesn't
    know about. Round-tripping through an older build must not drop them."""
    path = settings_store.settings_path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"future_thing": {"a": 1}, "arm": {"hotkey": "x"}}))
    settings_store.save(tmp_path, {"arm": {"hotkey": "y"}})
    loaded = settings_store.load(tmp_path)
    assert loaded["future_thing"] == {"a": 1}
    assert loaded["arm"]["hotkey"] == "y"


def test_load_ignores_malformed_json(tmp_path: Path) -> None:
    path = settings_store.settings_path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert settings_store.load(tmp_path) == {}


def test_load_ignores_non_object_json(tmp_path: Path) -> None:
    path = settings_store.settings_path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    assert settings_store.load(tmp_path) == {}


def test_load_config_overlays_user_settings(tmp_path: Path, monkeypatch) -> None:
    """The full path: save a hotkey to disk, load_config picks it up."""
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    # Make sure there's no env override for the hotkey
    monkeypatch.delenv("SAYZO_ARM__HOTKEY", raising=False)
    settings_store.save(tmp_path, {"arm": {"hotkey": "ctrl+alt+shift+r"}})

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.arm.hotkey == "ctrl+alt+shift+r"


def test_env_var_overrides_user_settings(tmp_path: Path, monkeypatch) -> None:
    """Env var precedence over user_settings.json must be preserved."""
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SAYZO_ARM__HOTKEY", "ctrl+shift+e")
    settings_store.save(tmp_path, {"arm": {"hotkey": "ctrl+alt+shift+r"}})

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.arm.hotkey == "ctrl+shift+e"
