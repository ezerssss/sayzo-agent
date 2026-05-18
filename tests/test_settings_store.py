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


def test_capture_system_scope_default_is_endpoint(tmp_path: Path, monkeypatch) -> None:
    """Default system_scope flipped from 'arm_app' to 'endpoint' in v2.9.0.

    Per-app capture is fragile across Chrome / OS / EDR configurations
    (see Sheen's Rippling-managed Mac that captured sys_total=0s on
    every meeting while Granola — global tap — worked fine). Endpoint
    capture is now the default; per-app is opt-in via Settings → Recording.
    """
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SAYZO_CAPTURE__SYSTEM_SCOPE", raising=False)

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.capture.system_scope == "endpoint"


def test_capture_system_scope_user_settings_overrides_default(
    tmp_path: Path, monkeypatch,
) -> None:
    """Users who opt into per-app via Settings → Recording (or by hand-editing
    user_settings.json) should keep their choice when the build's default
    flips."""
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SAYZO_CAPTURE__SYSTEM_SCOPE", raising=False)
    settings_store.save(tmp_path, {"capture": {"system_scope": "arm_app"}})

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.capture.system_scope == "arm_app"


def test_aec_enabled_default_is_false(tmp_path: Path, monkeypatch) -> None:
    """Fresh install: AEC is opt-in until a later v3.5.x patch flips the default."""
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SAYZO_AEC__ENABLED", raising=False)

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.aec.enabled is False


def test_aec_enabled_user_settings_persists_across_restart(
    tmp_path: Path, monkeypatch,
) -> None:
    """The bug that prompted v3.5.1: the Settings → Recording toggle wrote
    {"aec": {"enabled": true}} to user_settings.json but load_config()'s
    section allowlist didn't include "aec", so the value was silently
    dropped on the next launch. Regression-test it via the same write→
    load roundtrip the Settings UI performs."""
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SAYZO_AEC__ENABLED", raising=False)
    settings_store.save(tmp_path, {"aec": {"enabled": True}})

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.aec.enabled is True


def test_aec_env_var_wins_over_user_settings(tmp_path: Path, monkeypatch) -> None:
    """Standard precedence: SAYZO_AEC__ENABLED env var overrides user_settings.json."""
    monkeypatch.setenv("SAYZO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SAYZO_AEC__ENABLED", "false")
    settings_store.save(tmp_path, {"aec": {"enabled": True}})

    from sayzo_agent.config import load_config
    cfg = load_config()
    assert cfg.aec.enabled is False
