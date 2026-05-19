"""Tests for Settings → About → Install update flow (Stage 4).

Exercises the ``_install_update_worker`` synchronously by calling it directly
on the worker thread's behalf. The worker imports its dependencies inside the
function body (so circular imports stay broken on cold path), which means we
monkeypatch the source modules — ``sayzo_agent.update`` and
``sayzo_agent.update_stage`` — rather than the bridge module itself.

The bridge's outbound surface is two channels:
  - ``self._push_event`` -> captured into a list per test
  - ``self._ipc.call``   -> replaced with a recording mock

Both are easy to assert against without spinning up a real pywebview window
or a real IPC server.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sayzo_agent.config import Config
from sayzo_agent.gui.settings import bridge as bridge_mod
from sayzo_agent.gui.settings.bridge import Bridge
from sayzo_agent.gui.settings.ipc import IPCError, IPCNotConnected, Methods
from sayzo_agent.update import UpdateInfo
from sayzo_agent.update_stage import StagedUpdate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Real Config pointed at tmp_path so the worker can read/write real files."""
    return Config(data_dir=tmp_path)


@pytest.fixture
def bridge_with_capture(cfg: Config) -> tuple[Bridge, list[dict[str, Any]], MagicMock]:
    """Bridge wired with event-capture + a mock IPC client."""
    b = Bridge(cfg)
    captured: list[dict[str, Any]] = []
    b._push_event = captured.append  # type: ignore[assignment]
    b._ipc = MagicMock()  # type: ignore[assignment]
    return b, captured, b._ipc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _info(version: str, sha: str = "deadbeef") -> UpdateInfo:
    return UpdateInfo(
        version=version,
        url=f"https://sayzo.app/releases/Sayzo-{version}.bin",
        notes="Release notes here.",
        sha256=sha,
    )


def _staged(version: str, path: Path, sha: str = "deadbeef") -> StagedUpdate:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PRETEND-PAYLOAD")
    return StagedUpdate(
        version=version,
        platform="windows",
        sha256=sha,
        notes="Notes.",
        payload_path=path,
        ready_at="2026-05-12T00:00:00Z",
    )


def _phase_events(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("type") == "update_phase"]


# ---------------------------------------------------------------------------
# Already-staged shortcut
# ---------------------------------------------------------------------------


def test_install_when_already_staged_skips_download_and_calls_quit(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background poll already staged the latest version. Worker should not
    re-download; it should emit 'applying' then call QUIT_AGENT."""
    b, captured, ipc = bridge_with_capture

    staged = _staged("3.0.0", cfg.data_dir / "staged_update" / "payload.exe")

    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: staged
    )
    # Manifest agrees the staged version is the latest.
    monkeypatch.setattr(
        "sayzo_agent.update.check",
        _async_returning(_info("3.0.0")),
    )

    # download_and_stage MUST NOT be called in this branch.
    def _no_dl(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("download_and_stage should not run when already staged")

    monkeypatch.setattr("sayzo_agent.update_stage.download_and_stage", _no_dl)

    # Stub __version__ inside the bridge so is_newer treats us as older.
    monkeypatch.setattr(bridge_mod, "__version__", "2.0.0")

    b._install_update_worker()

    phases = _phase_events(captured)
    assert [e["phase"] for e in phases] == ["applying"]
    assert phases[0]["version"] == "3.0.0"
    ipc.call.assert_called_once_with(Methods.QUIT_AGENT)


def test_install_when_stale_stage_redownloads(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older stage is on disk; the manifest advertises something newer.
    The worker clears the stale stage and downloads the fresh one."""
    b, captured, ipc = bridge_with_capture
    stale = _staged("2.5.0", cfg.data_dir / "staged_update" / "payload.exe")

    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: stale
    )
    monkeypatch.setattr(
        "sayzo_agent.update.check",
        _async_returning(_info("3.0.0")),
    )

    cleared: list[Path] = []
    monkeypatch.setattr(
        "sayzo_agent.update_stage.clear_staged",
        lambda d: cleared.append(d),
    )

    fresh = _staged("3.0.0", cfg.data_dir / "staged_update" / "payload.exe")
    monkeypatch.setattr(
        "sayzo_agent.update_stage.download_and_stage",
        _async_returning(fresh),
    )
    monkeypatch.setattr(bridge_mod, "__version__", "2.0.0")

    b._install_update_worker()

    assert cleared == [cfg.data_dir]
    phases = [e["phase"] for e in _phase_events(captured)]
    # Downloading kickoff (percent=0), then applying after download completes.
    assert phases[0] == "downloading"
    assert "applying" in phases
    ipc.call.assert_called_once_with(Methods.QUIT_AGENT)


# ---------------------------------------------------------------------------
# Fresh-download path
# ---------------------------------------------------------------------------


def test_install_with_no_stage_downloads_then_applies(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, captured, ipc = bridge_with_capture

    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: None
    )
    monkeypatch.setattr(
        "sayzo_agent.update.check",
        _async_returning(_info("3.0.0")),
    )

    fresh = _staged("3.0.0", cfg.data_dir / "staged_update" / "payload.exe")

    async def _fake_download(
        info: UpdateInfo, data_dir: Path, *, progress_callback=None, **_kw: Any
    ) -> StagedUpdate:
        # Emit a couple of progress ticks so the test sees a downloading event
        # with non-zero percent — guards against an accidental reordering that
        # would push 'applying' before any progress.
        if progress_callback is not None:
            progress_callback(50, 100)
            progress_callback(100, 100)
        return fresh

    monkeypatch.setattr(
        "sayzo_agent.update_stage.download_and_stage", _fake_download
    )
    monkeypatch.setattr(bridge_mod, "__version__", "2.0.0")

    b._install_update_worker()

    phases = _phase_events(captured)
    kinds = [e["phase"] for e in phases]
    # Order must be: downloading (kickoff) -> downloading (50%, 100%) ->
    # applying. We don't assert exact percent values since the kickoff
    # always emits 0 first.
    assert kinds[0] == "downloading"
    assert kinds.count("downloading") >= 2
    assert kinds[-1] == "applying"
    # The applying event must carry the staged version so the UI can show
    # "Installing Sayzo X.Y.Z…".
    assert phases[-1]["version"] == "3.0.0"
    ipc.call.assert_called_once_with(Methods.QUIT_AGENT)


def test_install_no_update_available_emits_noop(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User clicked Install but the manifest reports we're already on the
    latest. UI shows the friendly "you're on latest" copy."""
    b, captured, ipc = bridge_with_capture

    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: None
    )
    monkeypatch.setattr(
        "sayzo_agent.update.check", _async_returning(None)
    )
    monkeypatch.setattr(bridge_mod, "__version__", "3.0.0")

    b._install_update_worker()

    phases = [e["phase"] for e in _phase_events(captured)]
    assert phases == ["noop_already_latest"]
    ipc.call.assert_not_called()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_install_download_failure_emits_error(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, captured, ipc = bridge_with_capture

    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: None
    )
    monkeypatch.setattr(
        "sayzo_agent.update.check",
        _async_returning(_info("3.0.0")),
    )
    monkeypatch.setattr(
        "sayzo_agent.update_stage.download_and_stage",
        _async_returning(None),
    )
    monkeypatch.setattr(bridge_mod, "__version__", "2.0.0")

    b._install_update_worker()

    phases = [e["phase"] for e in _phase_events(captured)]
    assert "error" in phases
    # Never tries to apply if download failed — would be a no-op anyway since
    # there's no staged payload, but the contract is explicit.
    ipc.call.assert_not_called()


def test_install_ipc_unreachable_emits_queued(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent isn't reachable when we want to trigger apply. The stage IS on
    disk, so the boot-time apply path will pick it up next launch — surface
    'queued_for_restart' rather than 'error' so the UI copy makes sense."""
    b, captured, ipc = bridge_with_capture

    staged = _staged("3.0.0", cfg.data_dir / "staged_update" / "payload.exe")
    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: staged
    )
    monkeypatch.setattr(
        "sayzo_agent.update.check",
        _async_returning(_info("3.0.0")),
    )
    monkeypatch.setattr(bridge_mod, "__version__", "2.0.0")

    ipc.call.side_effect = IPCNotConnected("agent down")

    b._install_update_worker()

    phases = [e["phase"] for e in _phase_events(captured)]
    assert phases[-1] == "queued_for_restart"


def test_install_ipc_error_emits_error(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, captured, ipc = bridge_with_capture
    staged = _staged("3.0.0", cfg.data_dir / "staged_update" / "payload.exe")
    monkeypatch.setattr(
        "sayzo_agent.update_stage.read_staged", lambda _d: staged
    )
    monkeypatch.setattr(
        "sayzo_agent.update.check",
        _async_returning(_info("3.0.0")),
    )
    monkeypatch.setattr(bridge_mod, "__version__", "2.0.0")

    ipc.call.side_effect = IPCError("server method raised")

    b._install_update_worker()

    phases = [e for e in _phase_events(captured) if e["phase"] == "error"]
    assert len(phases) == 1
    assert "server method raised" in phases[0]["message"]


# ---------------------------------------------------------------------------
# install_update_now public API surface
# ---------------------------------------------------------------------------


def test_install_update_now_returns_started_marker(
    bridge_with_capture: tuple[Bridge, list, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge method itself is fire-and-forget — it just spawns a
    worker. Assert the immediate return shape so the frontend can rely on
    it (settings-bridge.ts declares ``{ started: boolean }``)."""
    b, _captured, _ipc = bridge_with_capture
    # Block the worker so the test doesn't race with its thread.
    monkeypatch.setattr(b, "_install_update_worker", lambda: None)
    assert b.install_update_now() == {"started": True}


# ---------------------------------------------------------------------------
# Async helpers (stdlib threading doesn't play well with asyncio.run mocking)
# ---------------------------------------------------------------------------


def _async_returning(value: Any):
    """Returns a coroutine factory that always resolves to ``value``.

    The worker calls ``asyncio.run(_update_check(...))`` and
    ``asyncio.run(download_and_stage(...))`` inline; both expect awaitables.
    """
    async def _coro(*args: Any, **kwargs: Any) -> Any:
        return value
    return _coro


# ---------------------------------------------------------------------------
# Recording pane: get/set_recording_setting
# ---------------------------------------------------------------------------


def test_get_recording_settings_reflects_endpoint_default(cfg: Config) -> None:
    """Fresh config: per_app_capture defaults to False (endpoint scope is
    the default since v2.9.0); aec_enabled defaults to True (default flipped
    in v3.6.1 after the alignment fix landed)."""
    b = Bridge(cfg)
    assert b.get_recording_settings() == {
        "per_app_capture": False,
        "aec_enabled": True,
    }


def test_get_recording_settings_reflects_arm_app_override(cfg: Config) -> None:
    """User has opted into per-app capture (Beta toggle on)."""
    cfg.capture.system_scope = "arm_app"  # type: ignore[assignment]
    b = Bridge(cfg)
    assert b.get_recording_settings() == {
        "per_app_capture": True,
        "aec_enabled": True,
    }


def test_get_recording_settings_reflects_aec_disabled(cfg: Config) -> None:
    """User has turned AEC off from the Settings UI (default is on in v3.6.1+)."""
    cfg.aec.enabled = False
    b = Bridge(cfg)
    assert b.get_recording_settings() == {
        "per_app_capture": False,
        "aec_enabled": False,
    }


def test_set_recording_setting_enables_aec(cfg: Config) -> None:
    """Toggle on: cfg.aec.enabled mutates, user_settings.json gets aec.enabled,
    requires_restart=True is returned (APM is bound at first session-close)."""
    from sayzo_agent import settings_store as ss

    b = Bridge(cfg)
    result = b.set_recording_setting("aec_enabled", True)

    assert result == {"saved": True, "requires_restart": True}
    assert cfg.aec.enabled is True
    persisted = ss.load(cfg.data_dir)
    assert persisted == {"aec": {"enabled": True}}


def test_set_recording_setting_disables_aec(cfg: Config) -> None:
    """Toggle off: persists False explicitly so the user's choice survives
    even if the default flips on later."""
    from sayzo_agent import settings_store as ss

    cfg.aec.enabled = True
    b = Bridge(cfg)
    result = b.set_recording_setting("aec_enabled", False)

    assert result == {"saved": True, "requires_restart": True}
    assert cfg.aec.enabled is False
    persisted = ss.load(cfg.data_dir)
    assert persisted == {"aec": {"enabled": False}}


def test_set_recording_setting_enables_per_app(cfg: Config) -> None:
    """Toggle on: cfg mutates, user_settings.json gets the new scope,
    requires_restart=True is returned."""
    from sayzo_agent import settings_store as ss

    b = Bridge(cfg)
    result = b.set_recording_setting("per_app_capture", True)

    assert result == {"saved": True, "requires_restart": True}
    assert cfg.capture.system_scope == "arm_app"
    persisted = ss.load(cfg.data_dir)
    assert persisted == {"capture": {"system_scope": "arm_app"}}


def test_set_recording_setting_disables_per_app(cfg: Config) -> None:
    """Toggle off: persists 'endpoint' explicitly so the user's choice
    survives even if the default flips again later."""
    from sayzo_agent import settings_store as ss

    cfg.capture.system_scope = "arm_app"  # type: ignore[assignment]
    b = Bridge(cfg)
    result = b.set_recording_setting("per_app_capture", False)

    assert result == {"saved": True, "requires_restart": True}
    assert cfg.capture.system_scope == "endpoint"
    persisted = ss.load(cfg.data_dir)
    assert persisted == {"capture": {"system_scope": "endpoint"}}


def test_set_recording_setting_rejects_unknown_key(cfg: Config) -> None:
    """Defensive: future-self adds a new key in TS without wiring Python.
    We don't want a silent no-op + saved=True; we want a clear error."""
    b = Bridge(cfg)
    result = b.set_recording_setting("bogus", True)
    assert result["saved"] is False
    assert "unknown recording key" in result["error"]
    # cfg untouched.
    assert cfg.capture.system_scope == "endpoint"
