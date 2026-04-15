"""Speaker embedding and other-side clustering."""
from __future__ import annotations

import logging

import numpy as np

from .config import SpeakerConfig

log = logging.getLogger(__name__)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class SpeakerIdentifier:
    def __init__(self, cfg: SpeakerConfig) -> None:
        self.cfg = cfg
        self._encoder = None

    def _ensure_encoder(self):
        if self._encoder is not None:
            return self._encoder
        from resemblyzer import VoiceEncoder  # lazy
        self._encoder = VoiceEncoder()
        return self._encoder

    def embed(self, pcm_float32: np.ndarray) -> np.ndarray:
        encoder = self._ensure_encoder()
        from resemblyzer import preprocess_wav
        wav = preprocess_wav(pcm_float32, source_sr=16000)
        return encoder.embed_utterance(wav)

    def cluster_others(self, embeds: list[np.ndarray], merge_threshold: float = 0.75) -> list[int]:
        """Greedy online clustering by cosine similarity.

        Each new embedding joins the nearest existing cluster if cosine
        similarity to its centroid >= merge_threshold; otherwise opens a new
        cluster. Capped at `max_other_speakers` (extras fold into nearest).
        Avoids a sklearn dependency for the small N we deal with.
        """
        if not embeds:
            return []
        centroids: list[np.ndarray] = []
        counts: list[int] = []
        labels: list[int] = []
        max_k = self.cfg.max_other_speakers
        for e in embeds:
            best_idx = -1
            best_sim = -1.0
            for i, c in enumerate(centroids):
                sim = _cosine(e, c)
                if sim > best_sim:
                    best_sim = sim
                    best_idx = i
            if best_idx == -1 or (best_sim < merge_threshold and len(centroids) < max_k):
                centroids.append(e.copy())
                counts.append(1)
                labels.append(len(centroids) - 1)
            else:
                # update centroid as running mean
                counts[best_idx] += 1
                centroids[best_idx] = centroids[best_idx] + (e - centroids[best_idx]) / counts[best_idx]
                labels.append(best_idx)
        return labels
