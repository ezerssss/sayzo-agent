"""Notification copy composition for the daily drill.

Pure function ``compose_copy(resp)`` returning ``(title, body)``.

The user's product spec was explicit: never fire generic "Time to
practice!" copy. The notification text must be specific to today's drill
so it doesn't get filtered as generic noise. We use ``scenarioTitle``
when available; the replay variant gets a punchier "redo" framing.

The body uses ``question`` (the prompt the drill will ask the user)
truncated at a word boundary so the OS toast doesn't ellipsize mid-word.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import TodaySessionResponse


# Hard cap on body length. Both Windows toasts and macOS banners ellipsize
# beyond ~140-180 chars; we cut earlier and add a clean "…" so it doesn't
# look like the OS truncated us.
_BODY_MAX_CHARS = 140


# The exact phrase the user instructed us never to use.
_FORBIDDEN_GENERIC_TITLE = "Time to practice!"


def compose_copy(resp: "TodaySessionResponse") -> tuple[str, str]:
    """Return ``(title, body)`` for a daily-drill notification.

    Title selection:

    * ``is_replay=True`` → "A 60-sec redo from yesterday's meeting"
      (the replay framing matters — it's the moat of the product)
    * ``scenario_title`` set → "Your {scenario_title} drill is ready — 60s"
    * else → "Quick drill: 60 seconds"

    Body uses ``question`` truncated at ``_BODY_MAX_CHARS`` at word
    boundary; falls back to a generic "Open to start" prompt if no
    question text is present.

    Never returns the literal "Time to practice!" string for the title.
    """
    title = _compose_title(resp)
    assert title != _FORBIDDEN_GENERIC_TITLE, (
        "compose_copy regression: never emit the generic 'Time to practice!' string"
    )
    body = _compose_body(resp)
    return title, body


def _compose_title(resp: "TodaySessionResponse") -> str:
    if resp.is_replay:
        return "A 60-sec redo from yesterday's meeting"
    if resp.scenario_title:
        return f"Your {resp.scenario_title} drill is ready — 60s"
    return "Quick drill: 60 seconds"


def _compose_body(resp: "TodaySessionResponse") -> str:
    q = (resp.question or "").strip()
    if not q:
        return "Open to start your 60-second drill."
    return _truncate_at_word_boundary(q, _BODY_MAX_CHARS)


def _truncate_at_word_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Reserve 1 char for the ellipsis.
    cut = text[: max_chars - 1]
    last_space = cut.rfind(" ")
    if last_space > max_chars // 2:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


__all__ = ["compose_copy"]
