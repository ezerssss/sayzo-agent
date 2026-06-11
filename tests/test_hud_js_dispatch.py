"""Guards the HUD stdin→React JS dispatch against injection.

Pre-v3.14 ``window.py`` embedded the raw command JSON in a JS *template
literal* escaping only backslash + backtick, so any ``${...}`` in a toast /
insight body (server- or transcript-derived) executed as JavaScript. The
fix embeds via ``JSON.parse(json.dumps(raw))`` — a double-quoted string
literal with no backtick context. These tests pin that contract.
"""
from __future__ import annotations

import json

from sayzo_agent.gui.hud.js_escape import build_dispatch_js

# Payloads engineered to break a naive template-literal embedding.
HOSTILE = [
    '{"cmd":"show_toast","title":"hi","body":"${process.env.SECRET}"}',
    "`backtick` plus ${injection}",
    "line1\nline2\r\nline3",
    "U+2028  and U+2029  line separators",
    'emoji 🎉 and mixed quotes "\'',
    "</script><script>alert(1)</script>",
    "back\\slash and \"escaped\" quote",
    "",
]


def test_no_template_literal_embedding():
    """The argument to JSON.parse must be a double-quoted string literal,
    never a backtick template literal (the pre-v3.14 injection vector). A
    backtick *inside the data* is fine — it's inert in a double-quoted
    literal — so we assert the delimiter, not the absence of backticks."""
    for raw in HOSTILE:
        js = build_dispatch_js(raw)
        assert 'JSON.parse("' in js
        assert "JSON.parse(`" not in js


def test_uses_json_parse_and_dispatch():
    js = build_dispatch_js('{"cmd":"x"}')
    assert "JSON.parse(" in js
    assert "window.hudBridge.dispatch" in js


def test_embeds_json_literal_that_roundtrips():
    """The embedded literal must decode back to the exact original string —
    proves the escaping is correct without spinning up a JS engine."""
    for raw in HOSTILE:
        js = build_dispatch_js(raw)
        literal = json.dumps(raw)  # what build_dispatch_js embeds
        assert literal in js
        assert json.loads(literal) == raw
