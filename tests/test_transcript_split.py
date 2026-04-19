"""Unit tests for word-gap-based segment splitting.

Pure-Python tests — no audio / Whisper / OS dependencies. Exercises
``_split_segment_by_word_gaps`` which post-processes Whisper output to
tighten segment start times when Whisper groups words across a
turn-taking pause.
"""
from __future__ import annotations

from sayzo_agent.app import (
    TRANSCRIPT_WORD_GAP_SECS,
    _split_segment_by_word_gaps,
)
from sayzo_agent.stt import TranscribedSegment, Word


def _seg(words: list[tuple[float, float, str]], fallback_text: str = "") -> TranscribedSegment:
    ws = [Word(start=s, end=e, text=t) for s, e, t in words]
    start = ws[0].start if ws else 0.0
    end = ws[-1].end if ws else 0.0
    text = fallback_text or "".join(w.text for w in ws).strip()
    return TranscribedSegment(start=start, end=end, text=text, words=ws)


def test_segment_with_no_gaps_stays_whole():
    seg = _seg([(0.0, 0.3, " hello"), (0.35, 0.6, " there")])
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert len(out) == 1
    assert out[0][0] == 0.0
    assert out[0][1] == 0.6
    assert out[0][2] == "hello there"


def test_segment_with_internal_gap_splits():
    # "hmm" (0.0-0.3) then 1.5s pause then "yeah I think" (1.8-2.6)
    seg = _seg(
        [
            (0.0, 0.3, " hmm"),
            (1.8, 2.0, " yeah"),
            (2.1, 2.3, " I"),
            (2.35, 2.6, " think"),
        ]
    )
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert len(out) == 2
    assert out[0] == (0.0, 0.3, "hmm")
    assert out[1][0] == 1.8
    assert out[1][1] == 2.6
    assert out[1][2] == "yeah I think"


def test_segment_gap_exactly_at_threshold_does_not_split():
    # 0.8 s gap → threshold check is strict `>`, so exactly 0.8 stays together.
    seg = _seg([(0.0, 0.3, " a"), (1.1, 1.3, " b")])
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert len(out) == 1


def test_segment_gap_just_over_threshold_splits():
    seg = _seg([(0.0, 0.3, " a"), (1.15, 1.4, " b")])  # gap = 0.85
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert len(out) == 2


def test_multiple_gaps_produce_multiple_splits():
    seg = _seg(
        [
            (0.0, 0.2, " one"),
            (2.0, 2.2, " two"),  # gap 1.8
            (4.5, 4.7, " three"),  # gap 2.3
        ]
    )
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert len(out) == 3
    assert out[0][2] == "one"
    assert out[1][2] == "two"
    assert out[2][2] == "three"


def test_segment_without_words_falls_back_to_segment_bounds():
    seg = TranscribedSegment(start=1.0, end=5.0, text="opaque", words=[])
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert out == [(1.0, 5.0, "opaque")]


def test_segment_with_one_word_stays_whole():
    seg = _seg([(2.0, 2.5, " solo")])
    out = _split_segment_by_word_gaps(seg, 0.8)
    assert len(out) == 1
    assert out[0] == (2.0, 2.5, seg.text)


def test_default_threshold_is_sensible():
    """The module constant should be in the turn-taking range (~0.5-1.0s);
    shorter misses turn boundaries, longer catches intra-turn pauses."""
    assert 0.4 <= TRANSCRIPT_WORD_GAP_SECS <= 1.2
