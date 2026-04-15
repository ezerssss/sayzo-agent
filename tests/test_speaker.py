"""Tests for cosine helper and greedy other-speaker clustering."""
from __future__ import annotations

import numpy as np

from sayzo_agent.config import SpeakerConfig
from sayzo_agent.speaker import SpeakerIdentifier, _cosine


def test_cosine_basics():
    a = np.array([1.0, 0.0, 0.0])
    b = np.array([1.0, 0.0, 0.0])
    c = np.array([0.0, 1.0, 0.0])
    assert _cosine(a, b) == 1.0
    assert _cosine(a, c) == 0.0
    assert _cosine(np.zeros(3), a) == 0.0


def test_cluster_others_groups_similar():
    sp = SpeakerIdentifier(SpeakerConfig(max_other_speakers=4))
    a1 = np.array([1.0, 0.0, 0.0])
    a2 = np.array([0.99, 0.01, 0.0])
    b1 = np.array([0.0, 1.0, 0.0])
    b2 = np.array([0.01, 0.99, 0.0])
    labels = sp.cluster_others([a1, b1, a2, b2], merge_threshold=0.9)
    assert labels[0] == labels[2]
    assert labels[1] == labels[3]
    assert labels[0] != labels[1]


def test_cluster_others_caps_at_max():
    sp = SpeakerIdentifier(SpeakerConfig(max_other_speakers=2))
    embeds = [np.eye(4)[i] for i in range(4)]
    labels = sp.cluster_others(embeds, merge_threshold=0.99)
    assert max(labels) <= 1
