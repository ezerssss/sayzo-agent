"""First-launch marker file.

macOS has no equivalent of NSIS's finish-page ``MUI_FINISHPAGE_RUN`` — the
``.app`` just launches whenever the user double-clicks it, so we can't pass
a ``--force-setup`` flag on the very first open. Instead we write a marker
file after the first successful setup window close, and treat its absence
as "this is a first launch; always show the setup GUI regardless of what
detect_setup thinks."

The marker lives under ``cfg.data_dir`` so it moves with ``SAYZO_DATA_DIR``
and is swept away when the user blows away ``~/.sayzo/agent/`` to start
over — which is exactly the "act like a first-time user" expectation.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sayzo_agent.config import Config

log = logging.getLogger(__name__)

_MARKER_NAME = ".setup-seen"


def _marker_path(cfg: Config) -> Path:
    return cfg.data_dir / _MARKER_NAME


def is_first_launch(cfg: Config) -> bool:
    """Return True if the setup-seen marker is missing."""
    return not _marker_path(cfg).exists()


def mark_setup_seen(cfg: Config) -> None:
    """Create the marker so subsequent launches skip the forced GUI path."""
    path = _marker_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        log.warning("failed to write setup-seen marker at %s", path, exc_info=True)
