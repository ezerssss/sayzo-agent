"""Windows-specific first-run permission helpers.

The legacy WinRT toast-permission probes / openers are gone — the HUD
subprocess (`project_custom_hud_shipped`) owns the notification surface
end-to-end, so there's no OS permission for Sayzo to ask about.

Mic and WASAPI loopback don't surface a blocking OS dialog the way
macOS TCC does — if the user has mic privacy disabled in Windows
Settings, capture fails at runtime with a PortAudio error handled
upstream.
"""
from __future__ import annotations
