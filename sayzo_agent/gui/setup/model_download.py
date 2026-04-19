"""LLM weights download with a Python-callable progress hook.

Wraps :func:`huggingface_hub.hf_hub_download` and surfaces byte-level progress
to a caller-supplied callback. Used by the first-run GUI to drive the model
download progress bar; usable on its own from anywhere.

Implementation: pass a custom ``tqdm_class`` (a public parameter of
``hf_hub_download`` since huggingface-hub 1.x) whose ``.update`` invokes the
callback. The callback is bound via a :class:`contextvars.ContextVar` so the
custom class doesn't need to carry per-call state itself, and so concurrent
downloads in different threads stay isolated.
"""
from __future__ import annotations

import contextvars
import logging
from pathlib import Path
from typing import Callable

from huggingface_hub import hf_hub_download
from tqdm.auto import tqdm as _BaseTqdm

from sayzo_agent.config import Config

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
"""``(bytes_done, bytes_total)`` — total may be 0 if hub didn't pre-fetch size."""

_progress_cb: contextvars.ContextVar[ProgressCallback | None] = contextvars.ContextVar(
    "sayzo_download_progress_cb", default=None
)


class _ProgressTqdm(_BaseTqdm):
    """tqdm subclass that fans every ``update`` out to the active callback.

    Inheriting from ``tqdm.auto.tqdm`` keeps the rich/notebook detection
    behavior, all the bar-drawing, and any future tqdm features — we only
    add the callback fan-out. If hf_hub instantiates this with a total of
    None (unknown size) we still report whatever ``self.n`` reaches so the
    UI at least shows monotonic progress.
    """

    def update(self, n: int = 1) -> bool | None:
        result = super().update(n)
        cb = _progress_cb.get()
        if cb is not None:
            try:
                cb(int(self.n), int(self.total or 0))
            except Exception:
                log.warning("progress callback raised; continuing", exc_info=True)
        return result


def download_model_with_progress(
    cfg: Config, on_progress: ProgressCallback | None = None
) -> Path:
    """Download the configured LLM weights into ``cfg.models_dir``.

    Synchronous; blocks until the download completes. Resumable — repeated
    calls with a partially-downloaded file pick up where they left off
    (delegated to huggingface_hub's caching).

    ``on_progress`` is called from the download thread on every tqdm update.
    Throttling (drop-rate, time/percent gating) is the caller's responsibility
    so this function stays a thin wrapper.
    """
    token = _progress_cb.set(on_progress)
    try:
        log.info(
            "downloading %s/%s into %s",
            cfg.llm.repo_id,
            cfg.llm.filename,
            cfg.models_dir,
        )
        result = hf_hub_download(
            repo_id=cfg.llm.repo_id,
            filename=cfg.llm.filename,
            local_dir=str(cfg.models_dir),
            tqdm_class=_ProgressTqdm,
        )
        return Path(result)
    finally:
        _progress_cb.reset(token)
