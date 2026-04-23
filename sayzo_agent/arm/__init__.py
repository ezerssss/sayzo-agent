"""Armed-only capture model.

Submodules (lazy-imported — this package has heavy transitive deps like
pynput and pycaw that tests of a single pure submodule shouldn't pull in):

- ``controller``: ArmController state machine, hotkey wiring, check-ins,
  meeting-ended watcher.
- ``detectors``: pure matching logic around ``DetectorSpec``.
- ``hotkey``: pynput global hotkey listener + asyncio bridge.
- ``platform_win``: Windows mic-holder / foreground resolvers.
- ``platform_mac``: macOS mic-active / foreground / AppleScript URL resolvers.
- ``whitelist``: polling task that calls detectors while disarmed.
"""
from __future__ import annotations

__all__ = ["ArmController", "ArmReason", "ArmState"]


def __getattr__(name: str):
    # Lazy re-export from controller so `from sayzo_agent.arm import ArmController`
    # works without forcing an import of the whole subsystem at package-load time.
    if name in {"ArmController", "ArmReason", "ArmState"}:
        from .controller import ArmController, ArmReason, ArmState
        return {"ArmController": ArmController, "ArmReason": ArmReason, "ArmState": ArmState}[name]
    raise AttributeError(name)
