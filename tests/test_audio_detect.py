"""Tests for the audio-detect JSON parser in :mod:`sayzo_agent.arm.audio_detect`.

The Swift binary itself can only run on macOS in CI; what we exercise
here is the Python wrapper's JSON parsing — including forward-compat
with older binaries that don't emit the ``input_device_names`` field.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sayzo_agent.arm import audio_detect


@pytest.fixture(autouse=True)
def _reset_cache():
    audio_detect.reset_cache()
    yield
    audio_detect.reset_cache()


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["audio-detect", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_parses_input_device_names(monkeypatch):
    """v2.7.12+ binaries emit input_device_names; the wrapper threads
    them through to AudioProcess.input_device_names as a tuple."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(audio_detect, "_binary_path", lambda: Path("/fake/audio-detect"))

    fake_json = (
        '[{"pid":1234,"responsible_pid":1200,"bundle_id":"us.zoom.xos",'
        '"input":1,"output":0,"running":1,'
        '"input_device_names":["MacBook Pro Microphone","USB Headset"]}]'
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: _fake_completed(fake_json),
    )

    snapshot = audio_detect.snapshot(force_refresh=True)
    assert len(snapshot) == 1
    proc = snapshot[0]
    assert proc.pid == 1234
    assert proc.input is True
    assert proc.input_device_names == ("MacBook Pro Microphone", "USB Headset")


def test_forward_compat_when_input_device_names_missing(monkeypatch):
    """Older Swift binaries (pre v2.7.12) don't emit input_device_names.
    The wrapper must treat that as ``()`` rather than failing — agents
    in the field could be on a partially-upgraded layout (new Python
    code + old bundled Swift binary) during a staged rollout."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(audio_detect, "_binary_path", lambda: Path("/fake/audio-detect"))

    fake_json = (
        '[{"pid":1234,"responsible_pid":1200,"bundle_id":"us.zoom.xos",'
        '"input":1,"output":0,"running":1}]'
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: _fake_completed(fake_json),
    )

    snapshot = audio_detect.snapshot(force_refresh=True)
    assert len(snapshot) == 1
    assert snapshot[0].input_device_names == ()


def test_ignores_non_list_input_device_names(monkeypatch):
    """Defensive: a malformed binary that emits a non-list under the key
    must not crash the whole snapshot — log + treat as empty."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(audio_detect, "_binary_path", lambda: Path("/fake/audio-detect"))

    fake_json = (
        '[{"pid":1234,"responsible_pid":1200,"bundle_id":"us.zoom.xos",'
        '"input":1,"output":0,"running":1,"input_device_names":"oops"}]'
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: _fake_completed(fake_json),
    )

    snapshot = audio_detect.snapshot(force_refresh=True)
    assert len(snapshot) == 1
    assert snapshot[0].input_device_names == ()
