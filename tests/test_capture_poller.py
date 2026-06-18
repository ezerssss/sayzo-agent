"""Tests for sayzo_agent.capture_poller.CapturePoller.

The poller fires GET /api/captures/{id} on a sparse schedule after each
upload success. When the server reports a post-transcription status, the
poller caches title/summary into local record.json. We exercise the
schedule, the status gating, and the no-auth-client no-op fallback.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from sayzo_agent.auth.exceptions import AuthenticationRequired
from sayzo_agent.capture_poller import CapturePoller, _freshness_label
from sayzo_agent.models import ConversationRecord
from sayzo_agent.retry import empty_upload_state
from sayzo_agent.sink import read_record_from_dir, serialize_record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAuthClient:
    """Mimics AuthenticatedClient.get(path, ...). Each call pops the next
    scripted response off `responses`. A response is either a dict body
    (returned as status=200 JSON) or an Exception to raise."""

    def __init__(self) -> None:
        self.responses: list = []
        self.calls: list[str] = []

    def queue(self, body_or_exc) -> None:
        self.responses.append(body_or_exc)

    async def get(self, path: str, **kwargs) -> httpx.Response:
        self.calls.append(path)
        item = self.responses.pop(0) if self.responses else {}
        if isinstance(item, Exception):
            raise item
        # Construct a real httpx.Response so the poller's .raise_for_status()
        # and .json() paths exercise correctly.
        body = json.dumps(item).encode()
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )


def _write_capture(
    captures_dir: Path,
    rec_id: str,
    *,
    arm_app_key: str | None = "zoom",
    arm_app_display: str | None = "Zoom",
    local_clock_label: str = "2:30 pm",
) -> Path:
    """Create a capture dir + record.json fixture.

    Defaults model a whitelist-armed Zoom session — exercises the common
    path through ``_source_label`` (display_name + clock present).
    Override ``arm_app_*`` to None to test the hotkey fallback shape.
    """
    rec_dir = captures_dir / rec_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "close_reason": "joint_silence",
        "upload": empty_upload_state(),
        "local_clock_label": local_clock_label,
    }
    if arm_app_key:
        meta["arm_app_key"] = arm_app_key
    if arm_app_display:
        meta["arm_app_display"] = arm_app_display
    rec = ConversationRecord(
        id=rec_id,
        started_at=datetime(2026, 5, 14, 14, 32, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 14, 14, 47, 18, tzinfo=timezone.utc),
        title="Zoom call · 2026-05-14 14:32",  # local placeholder
        summary="",
        metadata=meta,
    )
    (rec_dir / "record.json").write_text(
        json.dumps(serialize_record(rec)), encoding="utf-8"
    )
    return rec_dir


@pytest.fixture
def env(tmp_path):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-poller")
    auth = FakeAuthClient()
    # Tiny real delays so the test runs in milliseconds without interfering
    # with pytest-asyncio's event-loop machinery (patching asyncio.sleep
    # globally broke status propagation between ticks).
    poller = CapturePoller(
        auth_client=auth,
        captures_dir=captures_dir,
        executor=executor,
        schedule=(0.001, 0.001, 0.001),
    )
    try:
        yield SimpleNamespace(
            captures_dir=captures_dir,
            executor=executor,
            auth=auth,
            poller=poller,
        )
    finally:
        executor.shutdown(wait=True)


def _read_record(rec_dir: Path) -> dict:
    return json.loads((rec_dir / "record.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_poller_caches_title_on_transcribed(env):
    rec_dir = _write_capture(env.captures_dir, "rec_t1")
    env.auth.queue({"status": "queued"})
    env.auth.queue({
        "status": "transcribed",
        "title": "Standup with backend team",
        "summary": "Discussed indexer rollout.",
    })
    env.auth.queue({"status": "analyzed"})  # terminal — stops polling

    await env.poller.poll(rec_dir, "srv_t1")
    data = _read_record(rec_dir)
    assert data["title"] == "Standup with backend team"
    assert data["summary"] == "Discussed indexer rollout."


async def test_poller_stops_on_terminal_status_without_overwrite(env):
    """If the server reaches a terminal status (rejected) without ever
    going past transcribed with non-empty title, the local placeholder
    stays untouched."""
    rec_dir = _write_capture(env.captures_dir, "rec_rejected")
    env.auth.queue({"status": "queued"})
    env.auth.queue({"status": "rejected"})
    # Extra queued responses must NOT be consumed.
    env.auth.queue({"status": "analyzed", "title": "should not appear"})

    await env.poller.poll(rec_dir, "srv_rejected")
    data = _read_record(rec_dir)
    assert data["title"] == "Zoom call · 2026-05-14 14:32"
    # The rejected response was the second call; the third was never made.
    assert len(env.auth.calls) == 2


async def test_poller_no_auth_client_is_noop(tmp_path):
    """When the agent runs against NoopUploadClient (signed-out), the
    poller is constructed with auth_client=None and must do nothing."""
    captures_dir = tmp_path / "captures_noop"
    captures_dir.mkdir()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        rec_dir = _write_capture(captures_dir, "rec_noop")
        poller = CapturePoller(
            auth_client=None,
            captures_dir=captures_dir,
            executor=executor,
            schedule=(0.0,),
        )
        await poller.poll(rec_dir, "srv_noop")
        # Placeholder unchanged.
        data = _read_record(rec_dir)
        assert data["title"] == "Zoom call · 2026-05-14 14:32"
    finally:
        executor.shutdown(wait=True)


async def test_poller_auth_required_aborts_quietly(env):
    """AuthenticationRequired from the auth client (token expired) ends
    polling without raising. Local placeholder stays."""
    rec_dir = _write_capture(env.captures_dir, "rec_auth")
    env.auth.queue(AuthenticationRequired("token expired"))
    env.auth.queue({"status": "analyzed", "title": "Never seen"})

    # Should not raise.
    await env.poller.poll(rec_dir, "srv_auth")
    data = _read_record(rec_dir)
    assert data["title"] == "Zoom call · 2026-05-14 14:32"
    # Only the first call was made.
    assert len(env.auth.calls) == 1


async def test_poller_writes_only_when_value_changes(env):
    """If the server's title equals the existing local title, no rewrite
    happens (avoid no-op write churn)."""
    rec_dir = _write_capture(env.captures_dir, "rec_same")
    env.auth.queue({
        "status": "transcribed",
        "title": "Zoom call · 2026-05-14 14:32",  # same as placeholder
        "summary": "",  # same as placeholder
    })
    env.auth.queue({"status": "analyzed"})

    mtime_before = (rec_dir / "record.json").stat().st_mtime_ns
    await env.poller.poll(rec_dir, "srv_same")
    mtime_after = (rec_dir / "record.json").stat().st_mtime_ns
    # The file may not be rewritten when nothing changed.
    assert mtime_after == mtime_before


# ---------------------------------------------------------------------------
# Post-capture coaching insight (owns_toast path, v3.10+)
# ---------------------------------------------------------------------------


class FakeNotifier:
    """Captures the post-capture toast calls the poller makes."""

    def __init__(self) -> None:
        self.insight_calls: list[dict] = []
        self.actionable_calls: list[dict] = []
        self.toast_calls: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> None:
        self.toast_calls.append((title, body))

    def notify_actionable(self, title, body, *, button_label, on_pressed,
                          expire_after_secs, on_expire=None,
                          secondary_button_label=None, on_secondary_pressed=None):
        self.actionable_calls.append({
            "title": title, "body": body, "button_label": button_label,
            "expire_after_secs": expire_after_secs,
        })
        return True

    def notify_insight(self, *, headline, body, source_label, freshness_label,
                       button_label, on_pressed, expire_after_secs, quote=None,
                       insight_type=None, on_expire=None,
                       secondary_button_label=None, on_secondary_pressed=None):
        self.insight_calls.append({
            "headline": headline, "body": body, "source_label": source_label,
            "freshness_label": freshness_label,
            "quote": quote, "insight_type": insight_type,
            "button_label": button_label,
            "secondary_button_label": secondary_button_label,
            "on_secondary_pressed": on_secondary_pressed,
        })
        return True


def _fake_cfg(tmp_path, *, feedback=True, master=True, server="https://sayzo.app"):
    return SimpleNamespace(
        notify_capture_feedback=feedback,
        notifications_enabled=master,
        data_dir=tmp_path,
        auth=SimpleNamespace(effective_server_url=server),
    )


def _toast_poller(env, cfg, notifier, *, armed_check=None, schedule=(0.001,) * 6):
    return CapturePoller(
        auth_client=env.auth,
        captures_dir=env.captures_dir,
        executor=env.executor,
        schedule=schedule,
        notifier=notifier,
        config=cfg,
        armed_check=armed_check,
    )


_INSIGHT_BODY = {
    "type": "rephrase",
    "headline": "A clearer way to give your update",
    "quote": "I think maybe we could possibly look into it?",
    "body": "Try stating it directly: “I recommend we look into it.”",
    "why": "Direct phrasing signals confidence.",
}


async def test_owns_toast_polls_past_transcribed_to_analyzed_and_fires_insight(env, tmp_path):
    """The insight only exists at ``analyzed``. The owns_toast poll must NOT
    stop at the title (transcribed) like the legacy path — it keeps going to
    analyzed, fires the InsightCard, and persists the insight to record.json."""
    rec_dir = _write_capture(env.captures_dir, "rec_insight")
    env.auth.queue({"status": "queued"})
    env.auth.queue({"status": "transcribed", "title": "Q4 planning sync"})
    env.auth.queue({"status": "analyzed", "title": "Q4 planning sync",
                    "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path), notifier)

    await poller.poll(rec_dir, "srv_insight", True)

    # Polled all the way to analyzed (3 GETs) — did not stop at transcribed.
    assert len(env.auth.calls) == 3
    assert len(notifier.insight_calls) == 1
    call = notifier.insight_calls[0]
    assert call["headline"] == "A clearer way to give your update"
    assert call["quote"] == "I think maybe we could possibly look into it?"
    # Source label is derived from agent-side arm metadata + clock label,
    # NOT from record.title. The server's title "Q4 planning sync" still
    # lives on record.title (drives Settings → Captures + the deep-link
    # hero card) but the chip is the "you just got off this call" cue,
    # so freshness + source > topical subject for recognition. Fixture
    # arm_app_display="Zoom", local_clock_label="2:30 pm" → "2:30 pm Zoom call".
    assert call["source_label"] == "2:30 pm Zoom call"
    # ended_at in the fixture is wall-clock-2026 (far past), but the
    # _freshness_label helper is called with datetime.now() at fire — for
    # a 2026-05-14-vintage fixture the elapsed will be a large number.
    # What we're verifying here is that the field is populated, not the
    # exact bucket — the bucket math is unit-tested in
    # test_freshness_label_buckets.
    assert isinstance(call["freshness_label"], str)
    assert call["freshness_label"]  # non-empty
    assert call["button_label"] == "See full feedback"
    assert call["secondary_button_label"] == "Stop showing these"
    assert notifier.actionable_calls == []  # no fallback
    # Persisted to record.json for durability / the Captures pane.
    rec = read_record_from_dir(rec_dir)
    assert rec.metadata["coaching_insight"]["headline"] == _INSIGHT_BODY["headline"]
    assert rec.metadata["coaching_insight"]["quote"] == _INSIGHT_BODY["quote"]


async def test_owns_toast_analyzed_without_insight_fires_fallback(env, tmp_path):
    """When the server reaches analyzed with coaching_insight=null, the poller
    fires the no-insight "feedback ready" toast — personalized to the call and
    deep-linking to the conversation page — so we keep the click-through we'd
    otherwise lose without an insight card."""
    rec_dir = _write_capture(env.captures_dir, "rec_noinsight")
    env.auth.queue({"status": "analyzed", "title": "Q4 sync", "coaching_insight": None})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path), notifier)

    await poller.poll(rec_dir, "srv_noinsight", True)

    assert notifier.insight_calls == []
    assert len(notifier.actionable_calls) == 1
    call = notifier.actionable_calls[0]
    # Title is personalized from the fixture's arm metadata via _source_label
    # (arm_app_display="Zoom", local_clock_label="2:30 pm" → "2:30 pm Zoom call").
    assert call["title"] == "Your 2:30 pm Zoom call is ready to review"
    assert call["body"] == "Replay it and see your coaching moments."
    assert call["button_label"] == "See feedback"
    assert call["expire_after_secs"] == 15.0


async def test_owns_toast_terminal_failure_fires_fallback(env, tmp_path):
    """A terminal failure (transcription_failed) ends the poll and fires the
    no-insight "feedback ready" toast — no insight will ever come."""
    rec_dir = _write_capture(env.captures_dir, "rec_failed")
    env.auth.queue({"status": "queued"})
    env.auth.queue({"status": "transcription_failed"})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path), notifier)

    await poller.poll(rec_dir, "srv_failed", True)

    assert notifier.insight_calls == []
    assert len(notifier.actionable_calls) == 1


async def test_owns_toast_feature_off_mid_poll_suppresses_everything(env, tmp_path):
    """If notify_capture_feedback flipped off during the poll, fire nothing —
    the immediate saved toast was already suppressed at upload time."""
    rec_dir = _write_capture(env.captures_dir, "rec_off")
    env.auth.queue({"status": "analyzed", "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path, feedback=False), notifier)

    await poller.poll(rec_dir, "srv_off", True)

    assert notifier.insight_calls == []
    assert notifier.actionable_calls == []


async def test_owns_toast_master_off_suppresses_everything(env, tmp_path):
    """Master notifications_enabled=False gates the insight path too."""
    rec_dir = _write_capture(env.captures_dir, "rec_master_off")
    env.auth.queue({"status": "analyzed", "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path, master=False), notifier)

    await poller.poll(rec_dir, "srv_master_off", True)

    assert notifier.insight_calls == []
    assert notifier.actionable_calls == []


async def test_owns_toast_defers_while_armed_then_fires_on_disarm(env, tmp_path, monkeypatch):
    """If the user is in ANOTHER meeting when the insight is ready, hold the
    toast and fire once they disarm."""
    monkeypatch.setattr("sayzo_agent.capture_poller._DEFER_POLL_SECS", 0.001)
    rec_dir = _write_capture(env.captures_dir, "rec_defer")
    env.auth.queue({"status": "analyzed", "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    state = {"n": 0}

    def armed_check() -> bool:
        state["n"] += 1
        return state["n"] <= 3  # armed for the first 3 checks, then disarmed

    poller = _toast_poller(env, _fake_cfg(tmp_path), notifier, armed_check=armed_check)
    await poller.poll(rec_dir, "srv_defer", True)

    assert len(notifier.insight_calls) == 1
    assert state["n"] >= 4  # we actually waited (polled armed state) before firing


async def test_owns_toast_dropped_when_armed_past_staleness_cap(env, tmp_path, monkeypatch):
    """Back-to-back meetings: if still armed past the staleness cap, drop the
    insight rather than firing it stale hours later."""
    monkeypatch.setattr("sayzo_agent.capture_poller._DEFER_POLL_SECS", 0.001)
    monkeypatch.setattr("sayzo_agent.capture_poller._INSIGHT_DEFER_MAX_SECS", 0.005)
    rec_dir = _write_capture(env.captures_dir, "rec_stale")
    env.auth.queue({"status": "analyzed", "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path), notifier, armed_check=lambda: True)

    await poller.poll(rec_dir, "srv_stale", True)

    assert notifier.insight_calls == []  # dropped as stale
    assert notifier.actionable_calls == []


async def test_stop_showing_button_disables_flag_and_persists(env, tmp_path):
    """The card's "Stop showing these" callback flips notify_capture_feedback
    off in-process AND persists it to user_settings.json (runs in the live
    agent process, so no IPC needed)."""
    import json as _json

    rec_dir = _write_capture(env.captures_dir, "rec_stop")
    env.auth.queue({"status": "analyzed", "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    cfg = _fake_cfg(tmp_path)
    poller = _toast_poller(env, cfg, notifier)

    await poller.poll(rec_dir, "srv_stop", True)
    assert len(notifier.insight_calls) == 1

    # Invoke the off-switch the user would click on the card.
    notifier.insight_calls[0]["on_secondary_pressed"]()

    assert cfg.notify_capture_feedback is False
    saved = _json.loads((tmp_path / "user_settings.json").read_text(encoding="utf-8"))
    assert saved["notify_capture_feedback"] is False
    # A small confirmation toast fired.
    assert any("no more insights" in t.lower() for t, _ in notifier.toast_calls)


async def test_non_owning_poll_does_not_fire_any_toast(env, tmp_path):
    """A sweep re-upload (owns_toast=False) caches title/summary but fires no
    toast even if the server has an insight ready."""
    rec_dir = _write_capture(env.captures_dir, "rec_sweep")
    env.auth.queue({"status": "analyzed", "title": "Q4 sync",
                    "coaching_insight": _INSIGHT_BODY})
    notifier = FakeNotifier()
    poller = _toast_poller(env, _fake_cfg(tmp_path), notifier)

    await poller.poll(rec_dir, "srv_sweep", False)

    assert notifier.insight_calls == []
    assert notifier.actionable_calls == []


# ---------------------------------------------------------------------------
# _freshness_label buckets (pure function)
# ---------------------------------------------------------------------------


def _at(secs_ago: float) -> tuple:
    """Build (ended_at, now) for ``_freshness_label`` such that elapsed == secs_ago."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    ended = datetime.fromtimestamp(now.timestamp() - secs_ago, tz=timezone.utc)
    return ended, now


def test_freshness_label_just_now_under_90s():
    # Common case: insight lands within ~minute of capture end.
    ended, now = _at(0)
    assert _freshness_label(ended, now=now) == "Just now"
    ended, now = _at(45)
    assert _freshness_label(ended, now=now) == "Just now"
    ended, now = _at(89)
    assert _freshness_label(ended, now=now) == "Just now"


def test_freshness_label_minutes_bucket():
    # Past the 90s threshold we report minutes.
    ended, now = _at(90)
    assert _freshness_label(ended, now=now) == "1 min ago"
    ended, now = _at(5 * 60)
    assert _freshness_label(ended, now=now) == "5 min ago"
    ended, now = _at(59 * 60 + 30)  # 59.5 min → 59 min (floored)
    assert _freshness_label(ended, now=now) == "59 min ago"


def test_freshness_label_hours_bucket():
    # Defer cap + poll ceiling can push past an hour in rare cases.
    ended, now = _at(60 * 60)
    assert _freshness_label(ended, now=now) == "1 hr ago"
    ended, now = _at(2 * 60 * 60 + 5 * 60)
    assert _freshness_label(ended, now=now) == "2 hr ago"


def test_freshness_label_handles_missing_ended_at():
    # Defensive: a record without ended_at shouldn't blow up the fire path.
    assert _freshness_label(None) == "Just now"


def test_freshness_label_handles_naive_datetime():
    # If something passes a naive datetime, we swallow + fall back rather
    # than crash the fire — the chip is glanceable copy, not load-bearing.
    assert _freshness_label(datetime(2026, 1, 1)) == "Just now"


# ---------------------------------------------------------------------------
# _source_label derivation paths (always from metadata, never from title)
# ---------------------------------------------------------------------------


def _rec(**meta_overrides) -> ConversationRecord:
    """Build a minimal record with the given metadata for source_label tests."""
    meta = {"local_clock_label": "2:30 pm"}
    meta.update(meta_overrides)
    return ConversationRecord(
        id="x",
        started_at=datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc),
        title="Q4 planning sync — server LLM title",  # MUST NOT appear in chip
        summary="",
        metadata=meta,
    )


def test_source_label_prefers_arm_app_display():
    rec = _rec(arm_app_display="Microsoft Teams", arm_app_key="teams_desktop")
    assert CapturePoller._source_label(rec) == "2:30 pm Microsoft Teams call"


def test_source_label_falls_back_to_app_key_title_case():
    # No display name (legacy / custom spec) → use the key.title() — produces
    # "Zoom" for "zoom" (fine), "Discord" for "discord" (fine). Knowingly
    # produces "Gmeet" for "gmeet" if display_name isn't set, but in
    # practice the default whitelist always sets display_name.
    rec = _rec(arm_app_key="zoom")
    assert CapturePoller._source_label(rec) == "2:30 pm Zoom call"


def test_source_label_hotkey_fallback_is_lowercase_conversation():
    # No arm app attribution (hotkey arm) → "conversation" so the chip
    # reads "from your 2:30 pm conversation" — lowercase 'c' so it
    # parses as a common noun mid-sentence, not a proper-noun "Conversation".
    rec = _rec()  # neither arm_app_display nor arm_app_key
    assert CapturePoller._source_label(rec) == "2:30 pm conversation"


def test_source_label_ignores_server_title():
    # The whole point of the refactor: the chip's wording is deterministic,
    # decoupled from whatever the server's title-generation produced. The
    # topical title still drives Settings → Captures + the deep-link page.
    rec = _rec(arm_app_display="Slack")
    assert CapturePoller._source_label(rec) == "2:30 pm Slack call"
    # Mutating record.title doesn't change the chip — chip never reads it.
    rec.title = "ANYTHING ELSE"
    assert CapturePoller._source_label(rec) == "2:30 pm Slack call"


def test_source_label_without_clock_label_falls_back_to_started_at():
    # Legacy record without cached clock label → recompute from started_at.
    rec = ConversationRecord(
        id="x",
        started_at=datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc),
        title="Zoom call · 2026-05-28 14:30",
        summary="",
        metadata={"arm_app_display": "Zoom"},
    )
    # Clock label is derived from local TZ, which varies per CI/dev machine.
    # Assert shape, not exact wall-clock — ends with "Zoom call", carries a
    # leading "{n}:{nn} {am|pm}" prefix.
    label = CapturePoller._source_label(rec)
    assert label.endswith(" Zoom call")
    head = label[: -len(" Zoom call")]
    assert head.endswith(" am") or head.endswith(" pm")


def test_source_label_handles_none_record():
    # Defensive: a record-read failure shouldn't crash the fire path; just
    # render a generic anchor.
    assert CapturePoller._source_label(None) == "conversation"
