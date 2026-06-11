"""Build the JS snippet that forwards a stdin command into the React app.

Isolated from ``window.py`` so the escaping contract is unit-testable
without importing Qt (the pytest suite is pure-Python by convention).

The payload is embedded as a *double-quoted JS string literal* produced
by ``json.dumps`` and decoded on the JS side with ``JSON.parse``. This
replaces the pre-v3.14 template-literal embedding, which escaped only
backslashes and backticks — leaving ``${...}`` interpolation live, so a
toast / insight body containing ``${...}`` (server- or transcript-
derived text) would execute as JavaScript inside the HUD. With
``json.dumps`` there is no backtick context at all and the escaping is
a strict superset of what a JS double-quoted literal requires
(``ensure_ascii=True`` also covers U+2028/U+2029, which are line
terminators in JS source but legal inside JSON strings).
"""
from __future__ import annotations

import json


def build_dispatch_js(raw_json: str) -> str:
    """Return a self-invoking JS expression dispatching ``raw_json``.

    ``raw_json`` is one newline-delimited JSON command exactly as the
    parent launcher wrote it to stdin. The JS side re-parses it with
    ``JSON.parse`` and hands the object to ``window.hudBridge.dispatch``.
    """
    return (
        "(function(){"
        "try{"
        f"const payload = JSON.parse({json.dumps(raw_json)});"
        "if (window.hudBridge && typeof window.hudBridge.dispatch === 'function') {"
        "  window.hudBridge.dispatch(payload);"
        "}"
        "}catch(e){console.warn('hud dispatch err', e);}"
        "})();"
    )
