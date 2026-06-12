"""Minimal ONNX Runtime wrapper for the vendored Silero VAD v5 model.

Numpy-only port of silero-vad's ``OnnxWrapper`` (MIT — see
https://github.com/snakers4/silero-vad). The upstream ``silero_vad``
package imports ``torch`` + ``torchaudio`` at module level even when you
only want its ONNX backend, which forced the agent to ship the entire
PyTorch runtime (~320 MB in the frozen bundle) to run a 2 MB model.
v3.17 vendors the model file at ``sayzo_agent/data/silero_vad.onnx`` and
runs it through ``onnxruntime`` directly; torch / torchaudio /
silero-vad are no longer dependencies.

The port is deliberately narrower than upstream: batch size is fixed at
1 and the only supported rate is 16 kHz with 512-sample chunks — the
only shape the agent ever feeds (see ``vad.py`` + ``echo_guard.py``).

History note: onnxruntime was the VAD runtime once before (pre-v3.0.1)
as a *transitive* dep of faster-whisper, and silently vanished from the
graph when faster-whisper was removed — that's the v3.0.0 regression
that motivated the torch-JIT switch. It is now a DIRECT dep in
pyproject.toml, and ``sayzo-agent healthcheck`` runs a real inference
through this module against the built bundle in CI, so the same class
of break fails the build instead of the user.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_MODEL_FILENAME = "silero_vad.onnx"


def model_path() -> Path:
    """Resolve the vendored .onnx file in both dev and frozen layouts."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS")) / "sayzo_agent" / "data"
    else:
        base = Path(__file__).parent / "data"
    return base / _MODEL_FILENAME


class SileroOnnxModel:
    """Stateful Silero VAD v5 session: 16 kHz mono, 512-sample chunks.

    Call with a 1-D float32 numpy chunk of exactly 512 samples; returns
    the speech probability as a plain float. Stateful across calls (the
    model carries an RNN state plus 64 samples of left context), so use
    one instance per audio stream and ``reset_states()`` between
    streams.
    """

    _CONTEXT = 64  # samples of left-context the v5 model expects
    _STATE_SHAPE = (2, 1, 128)

    def __init__(self, path: Path | str | None = None) -> None:
        # Lazy import: onnxruntime loads a sizeable native DLL — keep it
        # off the agent's boot path (same treatment as torch got, see
        # the v2.14 boot-perf pass).
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        # CPU explicitly — never let ORT auto-pick a GPU provider.
        self._session = onnxruntime.InferenceSession(
            str(path or model_path()),
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        self._sr_input = np.array(16000, dtype=np.int64)
        self.reset_states()

    def reset_states(self) -> None:
        self._state = np.zeros(self._STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, self._CONTEXT), dtype=np.float32)

    def __call__(self, chunk: np.ndarray, sample_rate: int = 16000) -> float:
        if sample_rate != 16000:
            raise ValueError(f"only 16 kHz is supported, got {sample_rate}")
        if chunk.ndim != 1 or chunk.shape[0] != 512:
            raise ValueError(
                f"expected a 1-D 512-sample chunk, got shape {chunk.shape}"
            )
        x = np.concatenate(
            [self._context, chunk.reshape(1, 512).astype(np.float32, copy=False)],
            axis=1,
        )
        out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr_input}
        )
        self._context = x[:, -self._CONTEXT:]
        return float(out[0, 0])
