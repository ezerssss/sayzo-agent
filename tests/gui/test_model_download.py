"""Unit tests for sayzo_agent.gui.setup.model_download.

Tests the progress-fanout wiring in isolation — does not actually download
the multi-GB Qwen model.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sayzo_agent.config import Config
from sayzo_agent.gui.setup import model_download as md


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_progress_tqdm_calls_active_callback() -> None:
    calls: list[tuple[int, int]] = []
    token = md._progress_cb.set(lambda done, total: calls.append((done, total)))
    try:
        bar = md._ProgressTqdm(total=100)
        bar.update(20)
        bar.update(30)
        bar.close()
    finally:
        md._progress_cb.reset(token)
    assert calls == [(20, 100), (50, 100)]


def test_progress_tqdm_no_callback_is_noop() -> None:
    """Without an active callback the bar updates normally and doesn't crash."""
    bar = md._ProgressTqdm(total=10)
    bar.update(5)
    bar.update(5)
    bar.close()  # must not raise


def test_progress_callback_exception_does_not_break_download() -> None:
    """A bad callback must not bubble up — it'd kill the download."""
    def bad_cb(done: int, total: int) -> None:
        raise RuntimeError("oops")

    token = md._progress_cb.set(bad_cb)
    try:
        bar = md._ProgressTqdm(total=10)
        bar.update(5)  # must not raise
        bar.close()
    finally:
        md._progress_cb.reset(token)


def test_download_passes_tqdm_class_and_resets_contextvar(cfg: Config) -> None:
    """Verify the wrapper hands tqdm_class to hf_hub and resets the cb on exit."""
    fake_path = cfg.models_dir / cfg.llm.filename
    fake_path.write_bytes(b"fake")
    captured: dict[str, object] = {}

    def fake_hub(**kwargs):
        captured.update(kwargs)
        # Simulate hf_hub creating a progress bar mid-download.
        bar = md._ProgressTqdm(total=10)
        bar.update(10)
        bar.close()
        return str(fake_path)

    received: list[tuple[int, int]] = []

    with patch.object(md, "hf_hub_download", side_effect=fake_hub):
        out = md.download_model_with_progress(
            cfg, on_progress=lambda d, t: received.append((d, t))
        )

    assert out == fake_path
    assert captured["repo_id"] == cfg.llm.repo_id
    assert captured["filename"] == cfg.llm.filename
    assert captured["tqdm_class"] is md._ProgressTqdm
    assert received == [(10, 10)]
    # ContextVar must be reset after the call.
    assert md._progress_cb.get() is None


def test_download_resets_contextvar_on_exception(cfg: Config) -> None:
    """If hf_hub raises, the ContextVar must still be reset."""
    def boom(**_kwargs):
        raise RuntimeError("network down")

    with patch.object(md, "hf_hub_download", side_effect=boom):
        with pytest.raises(RuntimeError, match="network down"):
            md.download_model_with_progress(cfg, on_progress=lambda d, t: None)

    assert md._progress_cb.get() is None
