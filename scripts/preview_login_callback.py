"""Open the PKCE localhost-callback pages in your default browser for
visual review.

Usage:
    python scripts/preview_login_callback.py            # both variants
    python scripts/preview_login_callback.py success    # only success
    python scripts/preview_login_callback.py error      # only error

The pages live at ``sayzo_agent/auth/pkce.py::_render_callback_page`` and
are normally served from a temporary localhost HTTP server during the
OAuth redirect. This script renders them straight to a temp file and
opens the file:// URL — no auth flow needed.
"""

from __future__ import annotations

import sys
import tempfile
import webbrowser
from pathlib import Path

from sayzo_agent.auth.pkce import _render_callback_page


def _write_and_open(name: str, body: bytes) -> Path:
    path = Path(tempfile.gettempdir()) / f"sayzo-callback-preview-{name}.html"
    path.write_bytes(body)
    webbrowser.open(path.as_uri())
    print(f"[preview] {name}: {path}")
    return path


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which not in {"success", "error", "both"}:
        print(f"unknown mode: {which!r}. Use success | error | both.")
        return 2

    if which in {"success", "both"}:
        _write_and_open("success", _render_callback_page(success=True))
    if which in {"error", "both"}:
        _write_and_open(
            "error",
            _render_callback_page(success=False, error_code="access_denied"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
