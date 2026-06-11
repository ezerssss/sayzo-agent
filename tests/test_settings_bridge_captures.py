"""Tests for the Settings bridge's platform-redirect surface:

``open_capture_feedback`` (the Captures "View feedback" button) and
``open_web_app`` (sidebar / signed-out Account / Captures empty state). Both
route through ``webbrowser.open`` — we monkeypatch it and assert the URL.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sayzo_agent.config import Config
from sayzo_agent.gui.settings import bridge as bridge_mod
from sayzo_agent.gui.settings.bridge import Bridge


def _bridge(tmp_path: Path) -> Bridge:
    return Bridge(Config(data_dir=tmp_path))


def _write_capture(captures_dir: Path, rec_id: str, *, metadata: dict) -> None:
    rec_dir = captures_dir / rec_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": rec_id,
        "started_at": "2026-06-12T10:00:00+00:00",
        "ended_at": "2026-06-12T10:02:00+00:00",
        "title": "Test",
        "summary": "",
        "metadata": metadata,
    }
    (rec_dir / "record.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture every webbrowser.open(url) the bridge fires."""
    urls: list[str] = []
    monkeypatch.setattr(bridge_mod.webbrowser, "open", lambda u: urls.append(u))
    return urls


def test_open_capture_feedback_opens_server_deep_link(
    tmp_path: Path, opened: list[str]
) -> None:
    b = _bridge(tmp_path)
    rec_id = "a" * 12
    _write_capture(
        b._cfg.captures_dir, rec_id,
        metadata={"upload": {"status": "uploaded", "server_capture_id": "SRV9"}},
    )
    assert b.open_capture_feedback(rec_id) == {"opened": True}
    base = b._cfg.auth.effective_server_url.rstrip("/")
    # The deep-link uses the SERVER id, not the local capture id.
    assert opened == [f"{base}/app/conversations/SRV9"]


def test_open_capture_feedback_rejects_invalid_id(
    tmp_path: Path, opened: list[str]
) -> None:
    b = _bridge(tmp_path)
    assert b.open_capture_feedback("../etc/passwd")["error"] == "invalid_id"
    assert opened == []


def test_open_capture_feedback_not_uploaded_yet(
    tmp_path: Path, opened: list[str]
) -> None:
    b = _bridge(tmp_path)
    rec_id = "b" * 12
    _write_capture(
        b._cfg.captures_dir, rec_id, metadata={"upload": {"status": "pending"}},
    )
    assert b.open_capture_feedback(rec_id) == {"opened": False, "error": "not_uploaded"}
    assert opened == []


def test_open_web_app_opens_effective_server_url(
    tmp_path: Path, opened: list[str]
) -> None:
    b = _bridge(tmp_path)
    b.open_web_app()
    assert opened == [b._cfg.auth.effective_server_url or "https://sayzo.app"]
