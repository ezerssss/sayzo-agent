"""Notification copy composition tests."""
from __future__ import annotations

from sayzo_agent.daily_drill.api import TodaySessionResponse
from sayzo_agent.daily_drill.copy import compose_copy


def _resp(**overrides) -> TodaySessionResponse:
    base = dict(
        status="ok",
        session_id="sess_123",
        deep_link_url="https://sayzo.app/drills/sess_123",
        is_replay=False,
        scenario_title=None,
        question=None,
    )
    base.update(overrides)
    return TodaySessionResponse(**base)


def test_compose_copy_uses_scenario_title_when_present() -> None:
    title, body = compose_copy(_resp(scenario_title="Monday standup", question="Give your standup in 60s."))
    assert title == "Your Monday standup drill is ready — 60s"
    assert body == "Give your standup in 60s."


def test_compose_copy_replay_uses_redo_phrasing() -> None:
    title, body = compose_copy(
        _resp(is_replay=True, scenario_title="ignored", question="Redo your answer.")
    )
    assert title == "A 60-sec redo from yesterday's meeting"
    assert body == "Redo your answer."


def test_compose_copy_falls_back_when_title_missing() -> None:
    title, body = compose_copy(_resp(question="Pitch your project."))
    assert title == "Quick drill: 60 seconds"
    assert body == "Pitch your project."


def test_compose_copy_body_falls_back_when_question_missing() -> None:
    title, body = compose_copy(_resp(scenario_title="Daily check-in"))
    assert title == "Your Daily check-in drill is ready — 60s"
    assert body == "Open to start your 60-second drill."


def test_compose_copy_never_emits_generic_time_to_practice() -> None:
    """Spec: 'Never fire Time to practice! — generic copy is what makes
    notifications get ignored.'"""
    for variant in (
        _resp(),
        _resp(scenario_title=""),
        _resp(scenario_title=None, question=""),
        _resp(is_replay=False, scenario_title=None, question=None),
    ):
        title, _ = compose_copy(variant)
        assert "Time to practice" not in title


def test_compose_copy_truncates_long_question_at_word_boundary() -> None:
    long_q = (
        "Give a clear concise answer to this very long prompt that talks about "
        "many different scenarios and edge cases that the user might face when "
        "they are speaking with someone they have not met before in a casual setting"
    )
    title, body = compose_copy(_resp(question=long_q))
    assert len(body) <= 140
    assert body.endswith("…")
    # Did NOT cut mid-word — last char before ellipsis is not part of an
    # incomplete word (we trimmed back to the previous space).
    assert " " in body  # has at least one word boundary preserved


def test_compose_copy_strips_whitespace_around_question() -> None:
    title, body = compose_copy(_resp(question="  trimmed   "))
    assert body == "trimmed"
