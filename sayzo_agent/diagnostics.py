"""Remote diagnostics: opt-out inventory headers + on-demand / crash log upload.

Gated entirely by ``Config.share_diagnostics`` (default ON, disclosed in the
onboarding Done screen + Settings). When that flag is False, NOTHING here
sends anything off the machine and no persistent install-id file is created.

Three surfaces:

  * :func:`diagnostics_headers` — three ``X-Agent-*`` headers piggybacked on
    the existing ``GET /api/me`` poll (app version + OS string + per-install
    id). This is the "who's on Mac/Windows and what version" roster; because
    it rides the account poll rather than the capture upload, it also surfaces
    users who never upload a capture (e.g. the Mac live-upload-fails cohort).

  * :class:`DiagnosticsUploader` — POSTs the agent's rotating ``agent.log``
    (plus any rotated backups) to ``/api/diagnostics/upload``. Two triggers,
    both one-shot and fire-and-forget: an on-demand pull (the server sets
    ``collect_logs`` in the /api/me body) and an automatic crash report (a
    sentinel written by the excepthook, swept on the next service boot).

  * :func:`write_crash_sentinel` / :func:`crash_sentinel_path` — the
    crash-report handshake between the dying process and the next boot.

The log payload is PII/content-free **by design** — ``agent.log`` never
contains transcripts, audio, auth tokens, or email content. Do not change that
just because we now upload it (see CLAUDE.md, "Notifications" + logging notes).
"""
from __future__ import annotations

import gzip
import json
import logging
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from . import __version__
from .upload import parse_json_body

if TYPE_CHECKING:
    from .auth.client import AuthenticatedClient
    from .config import Config

log = logging.getLogger(__name__)

DIAGNOSTICS_UPLOAD_PATH = "/api/diagnostics/upload"
INSTALL_ID_FILENAME = "install_id"
CRASH_SENTINEL_FILENAME = ".pending_crash_report"

# agent.log + up to 5 rotated backups (RotatingFileHandler backupCount=5 in
# __main__._setup_file_logging). Newest first so a size cap drops the oldest.
_LOG_BASENAMES = ("agent.log",) + tuple(f"agent.log.{i}" for i in range(1, 6))

# Belt-and-suspenders cap on a single gz part so a pathological/corrupt file
# can't blow out a home uplink. agent.log is capped at 10 MB raw (~1 MB gz).
_MAX_GZ_PART_BYTES = 12 * 1024 * 1024


def get_or_create_install_id(data_dir: Path) -> str:
    """Stable per-install UUID, persisted at ``data_dir/install_id``.

    Disambiguates the same signed-in user across two machines. Created lazily
    on first read so an opted-out user — who never builds diagnostics headers
    or metadata — never gets a persistent identifier written to disk.
    """
    path = data_dir / INSTALL_ID_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("[diagnostics] install_id read failed", exc_info=True)
    new_id = uuid.uuid4().hex
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id, encoding="utf-8")
    except OSError:
        log.debug("[diagnostics] install_id write failed", exc_info=True)
    return new_id


def build_platform_string() -> str:
    """``<sys.platform>;<platform.platform()>;py<pyver>`` —
    e.g. ``win32;Windows-10-10.0.19045-SP0;py3.12.4``.

    The backend parses this exact shape for the last-seen OS/version roster;
    keep the three ``;``-joined fields and the ``py`` prefix in sync with the
    server contract.
    """
    pyver = sys.version.split()[0]
    return f"{sys.platform};{platform.platform()};py{pyver}"


def diagnostics_headers(cfg: "Config") -> dict[str, str]:
    """Inventory headers for the ``/api/me`` poll, or ``{}`` when opted out.

    Returns the three ``X-Agent-*`` headers when ``cfg.share_diagnostics`` is
    on; an empty dict otherwise — so the caller sends nothing extra and no
    install-id file is created.
    """
    if not cfg.share_diagnostics:
        return {}
    return {
        "X-Agent-Version": __version__,
        "X-Agent-Platform": build_platform_string(),
        "X-Agent-Install-Id": get_or_create_install_id(cfg.data_dir),
    }


def build_meta(cfg: "Config", reason: str) -> dict:
    """Metadata JSON part for a log upload. Matches the backend's ``meta``
    contract: version / platform / install_id / reason / captured_at."""
    return {
        "version": __version__,
        "platform": build_platform_string(),
        "install_id": get_or_create_install_id(cfg.data_dir),
        "reason": reason,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def crash_sentinel_path(data_dir: Path) -> Path:
    return data_dir / CRASH_SENTINEL_FILENAME


def write_crash_sentinel(data_dir: Path, detail: str = "") -> None:
    """Mark that an unhandled exception happened so the next service boot
    uploads the log.

    Tiny + best-effort: called from the excepthook while the process is
    already dying, so it MUST never raise. The boot sweep
    (:class:`DiagnosticsUploader`) reads only the file's existence; the body
    is a human-readable breadcrumb, not parsed.
    """
    try:
        crash_sentinel_path(data_dir).write_text(detail or "crash", encoding="utf-8")
    except Exception:
        pass


def _collect_log_parts(logs_dir: Path) -> list[tuple[str, tuple[str, bytes, str]]]:
    """gzip each existing log file into an httpx multipart part under field
    name ``log``.

    Per-file try/except so a locked/rotating/oversized file is skipped rather
    than failing the whole upload. Returns ``[]`` when there's nothing to send.
    """
    parts: list[tuple[str, tuple[str, bytes, str]]] = []
    for name in _LOG_BASENAMES:
        p = logs_dir / name
        try:
            raw = p.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            log.debug("[diagnostics] could not read %s", name, exc_info=True)
            continue
        if not raw:
            continue
        gz = gzip.compress(raw)
        if len(gz) > _MAX_GZ_PART_BYTES:
            log.warning(
                "[diagnostics] skipping oversized log part %s (%d gz bytes)",
                name, len(gz),
            )
            continue
        parts.append(("log", (f"{name}.gz", gz, "application/gzip")))
    return parts


class DiagnosticsUploader:
    """POST the agent's logs to ``/api/diagnostics/upload``.

    One-shot and fire-and-forget — there is no in-process retry loop. The
    natural retry is the trigger itself: the server clears the on-demand
    ``collect_logs`` flag only when it *receives* the upload, and the crash
    sentinel is deleted only on success, so a failed attempt is simply
    re-attempted on the next poll / next boot.

    :meth:`upload` raises on failure (so a caller can branch on it);
    :meth:`try_upload` swallows + logs and returns a bool for the common
    fire-and-forget case.
    """

    def __init__(self, auth_client: "AuthenticatedClient", cfg: "Config") -> None:
        self._client = auth_client
        self._cfg = cfg

    async def upload(self, reason: str) -> dict | None:
        parts = _collect_log_parts(self._cfg.logs_dir)
        if not parts:
            log.info("[diagnostics] no log files to upload (reason=%s)", reason)
            return None
        meta = build_meta(self._cfg, reason)
        resp = await self._client.post(
            DIAGNOSTICS_UPLOAD_PATH,
            data={"meta": json.dumps(meta, ensure_ascii=False)},
            files=parts,
            headers={"X-Agent-Version": __version__},
            timeout=httpx.Timeout(60.0),
        )
        resp.raise_for_status()
        log.info(
            "[diagnostics] uploaded %d log part(s) reason=%s status=%s",
            len(parts), reason, resp.status_code,
        )
        return parse_json_body(resp)

    async def try_upload(self, reason: str) -> bool:
        """Best-effort: returns True on success, False on any error (logged,
        never raised)."""
        try:
            await self.upload(reason)
            return True
        except Exception:
            log.warning(
                "[diagnostics] upload failed (reason=%s)", reason, exc_info=True
            )
            return False
