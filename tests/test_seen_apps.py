"""Tests for the seen-apps persistence module.

Covers:
- Round-trip record → load → file-on-disk shape
- Dedup by lower-cased key (repeat observations bump count + timestamp)
- Already-whitelisted keys are skipped on write and filtered on read
- Cap-and-evict behavior when more than ``_MAX_ENTRIES`` unique keys
- ``dismiss`` removes the entry from disk
- Malformed file / missing file / wrong version → empty load
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sayzo_agent.arm import seen_apps
from sayzo_agent.arm.seen_apps import SeenApp
from sayzo_agent.config import DetectorSpec, default_detector_specs


def _whitelist_loom() -> list[DetectorSpec]:
    """A whitelist that already contains ``loom.exe`` — used to check
    scrubbing on load."""
    return [
        DetectorSpec(
            app_key="loom",
            display_name="Loom",
            process_names=["loom.exe"],
        ),
    ]


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_record_then_load_roundtrip(tmp_path: Path):
    seen_apps.record(
        tmp_path,
        key="loom.exe",
        display_name="Loom",
        whitelist=[],
        process_name="loom.exe",
    )
    entries = seen_apps.load(tmp_path, whitelist=[])
    assert len(entries) == 1
    e = entries[0]
    assert e.key == "loom.exe"
    assert e.display_name == "Loom"
    assert e.process_name == "loom.exe"
    assert e.seen_count == 1
    assert e.first_seen_ts > 0
    assert e.last_seen_ts >= e.first_seen_ts


def test_repeat_record_bumps_count_not_duplicate(tmp_path: Path):
    seen_apps.record(
        tmp_path, key="Loom.exe", display_name="Loom", whitelist=[],
        process_name="Loom.exe", now_ts=100.0,
    )
    seen_apps.record(
        tmp_path, key="loom.exe", display_name="Loom", whitelist=[],
        process_name="loom.exe", now_ts=200.0,
    )
    entries = seen_apps.load(tmp_path, whitelist=[])
    assert len(entries) == 1  # casing collapsed into one
    assert entries[0].seen_count == 2
    assert entries[0].last_seen_ts == 200.0
    assert entries[0].first_seen_ts == 100.0


def test_already_whitelisted_is_not_recorded(tmp_path: Path):
    seen_apps.record(
        tmp_path, key="loom.exe", display_name="Loom",
        whitelist=_whitelist_loom(), process_name="loom.exe",
    )
    # Nothing should have been written.
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_already_whitelisted_is_scrubbed_on_load(tmp_path: Path):
    """A key written before the user added it to the whitelist must be
    filtered out on subsequent loads — the Suggested section never
    re-offers an app the user already has."""
    seen_apps.record(
        tmp_path, key="loom.exe", display_name="Loom", whitelist=[],
        process_name="loom.exe",
    )
    assert len(seen_apps.load(tmp_path, whitelist=[])) == 1
    assert seen_apps.load(tmp_path, whitelist=_whitelist_loom()) == []


def test_disabled_whitelist_spec_also_scrubs(tmp_path: Path):
    """Even a disabled detector counts as 'already on the list'."""
    seen_apps.record(
        tmp_path, key="loom.exe", display_name="Loom", whitelist=[],
        process_name="loom.exe",
    )
    disabled = [
        DetectorSpec(
            app_key="loom",
            display_name="Loom",
            process_names=["loom.exe"],
            disabled=True,
        ),
    ]
    assert seen_apps.load(tmp_path, whitelist=disabled) == []


def test_macos_bundle_id_scrubbing(tmp_path: Path):
    """macOS entries use bundle id as key — scrubbing should match on
    ``bundle_ids`` as well as process names."""
    seen_apps.record(
        tmp_path, key="com.loom.desktop", display_name="Loom", whitelist=[],
        bundle_id="com.loom.desktop",
    )
    scrubbed = [
        DetectorSpec(
            app_key="loom",
            display_name="Loom",
            bundle_ids=["com.loom.desktop"],
        ),
    ]
    assert seen_apps.load(tmp_path, whitelist=scrubbed) == []


def test_cap_evicts_oldest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When more than _MAX_ENTRIES unique keys are recorded, the oldest
    ``last_seen_ts`` is evicted. The user-facing section in Settings
    doesn't need hundreds of entries; bounding the file also keeps the
    load cheap."""
    cap = seen_apps._MAX_ENTRIES
    for i in range(cap + 3):
        seen_apps.record(
            tmp_path, key=f"app{i}.exe",
            display_name=f"App {i}",
            whitelist=[],
            process_name=f"app{i}.exe",
            now_ts=1000.0 + i,
        )
    entries = seen_apps.load(tmp_path, whitelist=[])
    assert len(entries) == cap
    # The oldest 3 should be gone.
    keys = {e.key for e in entries}
    assert "app0.exe" not in keys
    assert "app1.exe" not in keys
    assert "app2.exe" not in keys
    assert f"app{cap + 2}.exe" in keys


def test_load_returns_most_recent_first(tmp_path: Path):
    seen_apps.record(
        tmp_path, key="a.exe", display_name="A", whitelist=[],
        process_name="a.exe", now_ts=100.0,
    )
    seen_apps.record(
        tmp_path, key="b.exe", display_name="B", whitelist=[],
        process_name="b.exe", now_ts=200.0,
    )
    seen_apps.record(
        tmp_path, key="c.exe", display_name="C", whitelist=[],
        process_name="c.exe", now_ts=150.0,
    )
    entries = seen_apps.load(tmp_path, whitelist=[])
    assert [e.key for e in entries] == ["b.exe", "c.exe", "a.exe"]


def test_dismiss_removes_entry(tmp_path: Path):
    seen_apps.record(
        tmp_path, key="a.exe", display_name="A", whitelist=[],
        process_name="a.exe",
    )
    seen_apps.record(
        tmp_path, key="b.exe", display_name="B", whitelist=[],
        process_name="b.exe",
    )
    seen_apps.dismiss(tmp_path, "a.exe")
    remaining = seen_apps.load(tmp_path, whitelist=[])
    assert len(remaining) == 1
    assert remaining[0].key == "b.exe"


def test_dismiss_is_case_insensitive(tmp_path: Path):
    seen_apps.record(
        tmp_path, key="LOOM.EXE", display_name="Loom", whitelist=[],
        process_name="LOOM.EXE",
    )
    seen_apps.dismiss(tmp_path, "loom.exe")
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_dismiss_missing_file_is_noop(tmp_path: Path):
    # Should not raise.
    seen_apps.dismiss(tmp_path, "whatever.exe")


def test_dismiss_is_permanent_across_rerecord(tmp_path: Path):
    """Dismiss must outlive the next observation of the same app — a user
    who said "no" shouldn't see the suggestion come back just because the
    agent restarted and saw the app hold the mic again."""
    seen_apps.record(
        tmp_path, key="obs64.exe", display_name="OBS", whitelist=[],
        process_name="obs64.exe",
    )
    seen_apps.dismiss(tmp_path, "obs64.exe")
    # Simulate a fresh observation (what the watcher would do next boot).
    seen_apps.record(
        tmp_path, key="obs64.exe", display_name="OBS", whitelist=[],
        process_name="obs64.exe",
    )
    # Still nothing to show — dismissal won.
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_dismiss_is_case_insensitive_across_observations(tmp_path: Path):
    seen_apps.dismiss(tmp_path, "OBS64.EXE")
    seen_apps.record(
        tmp_path, key="obs64.exe", display_name="OBS", whitelist=[],
        process_name="obs64.exe",
    )
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_undismiss_lets_app_reappear(tmp_path: Path):
    """After undismiss, future observations persist again. Used by the
    add-app flow so re-adding an app the user had dismissed clears the
    ban — if they wanted it back enough to add, we shouldn't suppress it
    forever."""
    seen_apps.record(
        tmp_path, key="obs64.exe", display_name="OBS", whitelist=[],
        process_name="obs64.exe",
    )
    seen_apps.dismiss(tmp_path, "obs64.exe")
    seen_apps.undismiss(tmp_path, "obs64.exe")
    seen_apps.record(
        tmp_path, key="obs64.exe", display_name="OBS", whitelist=[],
        process_name="obs64.exe",
    )
    entries = seen_apps.load(tmp_path, whitelist=[])
    assert len(entries) == 1
    assert entries[0].key == "obs64.exe"


def test_undismiss_missing_key_is_noop(tmp_path: Path):
    # Should not raise on an empty file / missing key.
    seen_apps.undismiss(tmp_path, "never.exe")
    seen_apps.record(
        tmp_path, key="loom.exe", display_name="Loom", whitelist=[],
    )
    # No dismissal ever existed → load returns the recorded entry.
    entries = seen_apps.load(tmp_path, whitelist=[])
    assert len(entries) == 1 and entries[0].key == "loom.exe"


def test_malformed_json_returns_empty(tmp_path: Path):
    path = tmp_path / "seen_apps.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_wrong_version_returns_empty(tmp_path: Path):
    path = tmp_path / "seen_apps.json"
    path.write_text(
        json.dumps({"version": 99, "entries": [{"key": "x", "display_name": "X"}]}),
        encoding="utf-8",
    )
    assert seen_apps.load(tmp_path, whitelist=[]) == []


def test_empty_key_record_is_noop(tmp_path: Path):
    seen_apps.record(
        tmp_path, key="", display_name="Empty", whitelist=[],
    )
    assert seen_apps.load(tmp_path, whitelist=[]) == []


# ---- display name heuristics -------------------------------------------


@pytest.mark.parametrize("proc, expected", [
    ("loom.exe", "Loom"),
    ("Discord.exe", "Discord"),
    ("RCMeetings.exe", "RCMeetings"),
    ("ms-teams.exe", "Ms Teams"),
    ("my_app_123", "My App 123"),
])
def test_display_name_for_process(proc: str, expected: str):
    assert seen_apps._display_name_for_process(proc) == expected


@pytest.mark.parametrize("bundle, expected", [
    ("com.hnc.Discord", "Discord"),
    ("us.zoom.xos", "Xos"),
    ("com.apple.FaceTime", "FaceTime"),
])
def test_display_name_for_bundle(bundle: str, expected: str):
    assert seen_apps._display_name_for_bundle(bundle) == expected
