"""User notifications via the custom HUD (v2.10+).

Sayzo used to route notifications through the OS notification surface
(WinRT on Windows; UNUserNotificationCenter / NSUserNotification / an
osascript consent modal on macOS). Every one of those produced a "no
toast appeared" incident at some point in v2.1–v2.7: AUMID drift,
Focus mode dropping banners, unsigned-bundle silent denial, TCC
entries going stale across signing changes. v2.10 retires the OS
surface entirely and renders every notification through a frameless
pywebview HUD window we own end-to-end (see :mod:`sayzo_agent.gui.hud`).

This module preserves the historical ``Notifier`` Protocol so the
ArmController, daily-drill scheduler, upload-retry manager, and app
orchestrator don't need to change. The two implementations are:

* :class:`HudNotifier` — wraps a :class:`HudLauncher` and forwards
  every notification through the HUD subprocess.
* :class:`NoopNotifier` — silent fallback used when
  ``SAYZO_NOTIFICATIONS_ENABLED=0`` or under unit tests.

The user-facing ``[notify] ...`` log shapes from earlier versions are
emitted by :class:`sayzo_agent.gui.hud.launcher.HudLauncher` at the
equivalent lifecycle points — ``notify scheduled``, ``ask scheduled``,
``ask resolved``, ``actionable scheduled``, ``actionable pressed``,
``actionable expired``. The granular OS-level diagnostics that lived
in the legacy ``DesktopNotifier`` (``send done`` / ``send failed`` /
``auth probe`` / ``bundle is_signed=...``) are gone — the HUD owns
its own surface, so there's no equivalent OS event to log. ``[hud] ...``
lines in :mod:`sayzo_agent.gui.hud.launcher` cover the HUD subprocess
lifecycle that replaces those.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Literal, Optional, Protocol

log = logging.getLogger(__name__)


ConsentResult = Literal["yes", "no", "timeout"]


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
    ) -> ConsentResult: ...

    def notify_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool: ...

    def notify_insight(
        self,
        *,
        headline: str,
        body: str,
        source_label: str,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        quote: Optional[str] = None,
        insight_type: Optional[str] = None,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool: ...

    def has_authorisation_sync(self) -> Optional[bool]: ...


class NoopNotifier:
    """Silent fallback. Returns sensible defaults and logs at debug.

    Used by unit tests (no HUD subprocess required) and when the user
    disables notifications via ``SAYZO_NOTIFICATIONS_ENABLED=0``.
    """

    def notify(self, title: str, body: str) -> None:
        log.debug("[notify] (noop) %s — %s", title, body)

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "timeout",
    ) -> ConsentResult:
        log.debug(
            "[notify] (noop) consent %s — %s → %s", title, body, default_on_timeout,
        )
        return default_on_timeout

    def notify_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        log.debug(
            "[notify] (noop) actionable %s — %s [%s]%s",
            title,
            body,
            button_label,
            f" / [{secondary_button_label}]" if secondary_button_label else "",
        )
        # Test paths can drive the expire branch by registering on_expire.
        if on_expire is not None:
            try:
                on_expire()
            except Exception:
                log.debug("[notify] (noop) on_expire raised", exc_info=True)
        return False

    def notify_insight(
        self,
        *,
        headline: str,
        body: str,
        source_label: str,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        quote: Optional[str] = None,
        insight_type: Optional[str] = None,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        log.debug(
            "[notify] (noop) insight %s — %s [%s]%s",
            headline,
            body,
            button_label,
            f" / [{secondary_button_label}]" if secondary_button_label else "",
        )
        if on_expire is not None:
            try:
                on_expire()
            except Exception:
                log.debug("[notify] (noop) insight on_expire raised", exc_info=True)
        return False

    def has_authorisation_sync(self) -> Optional[bool]:
        return None


class HudNotifier:
    """Routes every notification through the custom HUD subprocess.

    Thin wrapper around :class:`HudLauncher` — the launcher emits the
    ``[notify] ...`` log lines itself, so this class is effectively a
    Protocol-conformant adapter. ``has_authorisation_sync`` returns
    ``True`` whenever the HUD is alive: unlike OS notifications there's
    no permission state to query.
    """

    # Default TTL for fire-and-forget toasts. Matches the visual feel of
    # the old WinRT toasts (which auto-dismissed in roughly the same
    # window). Tunable per call by passing through to ``show_toast``.
    DEFAULT_TOAST_TTL_SECS = 4.0

    def __init__(self, launcher: "Any") -> None:
        # Imported as Any to avoid a circular type dependency
        # (gui.hud.launcher imports from notify? no — but keeping it
        # loose lets tests inject a fake without subclassing).
        self._launcher = launcher

    @property
    def launcher(self) -> "Any":
        """Expose the underlying :class:`HudLauncher` for non-Notifier callers.

        The ArmController uses this to drive the persistent capture pill
        (``show_pill`` / ``hide_pill`` / stop-button callback) — those
        commands aren't part of the Notifier Protocol because they're
        agent-state mirroring, not user notifications. Callers that
        only have a generic ``Notifier`` should use ``getattr(notifier,
        "launcher", None)`` and degrade gracefully when the underlying
        notifier is :class:`NoopNotifier` (no launcher).
        """
        return self._launcher

    def notify(self, title: str, body: str) -> None:
        self._launcher.show_toast(title, body, ttl_secs=self.DEFAULT_TOAST_TTL_SECS)

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
    ) -> ConsentResult:
        return self._launcher.ask_consent(
            title=title,
            body=body,
            yes_label=yes_label,
            no_label=no_label,
            timeout_secs=timeout_secs,
            default_on_timeout=default_on_timeout,
        )

    def notify_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        return self._launcher.show_actionable(
            title,
            body,
            button_label=button_label,
            on_pressed=on_pressed,
            expire_after_secs=expire_after_secs,
            on_expire=on_expire,
            secondary_button_label=secondary_button_label,
            on_secondary_pressed=on_secondary_pressed,
        )

    def notify_insight(
        self,
        *,
        headline: str,
        body: str,
        source_label: str,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        quote: Optional[str] = None,
        insight_type: Optional[str] = None,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        return self._launcher.show_insight(
            headline=headline,
            body=body,
            source_label=source_label,
            button_label=button_label,
            on_pressed=on_pressed,
            expire_after_secs=expire_after_secs,
            quote=quote,
            insight_type=insight_type,
            on_expire=on_expire,
            secondary_button_label=secondary_button_label,
            on_secondary_pressed=on_secondary_pressed,
        )

    def has_authorisation_sync(self) -> Optional[bool]:
        # The HUD is "authorised" by definition — we own the surface.
        # ``None`` is returned only when the launcher has given up after
        # repeated crashes; callers (daily-drill scheduler) treat ``None``
        # as "unknown" and skip the silent-drop fallback.
        try:
            return True if self._launcher.is_alive() else None
        except Exception:
            return None

    def diagnose(self) -> dict[str, Any]:
        """Surface the launcher's structured diagnostic.

        Consumed by ``sayzo-agent diagnose-notifications``.
        """
        try:
            return self._launcher.diagnose()
        except Exception:
            log.warning("[notify] diagnose failed", exc_info=True)
            return {"alive": False, "error": "diagnose raised"}


def make_notifier(launcher: Optional["Any"] = None) -> Notifier:
    """Construct the right notifier for the agent's current state.

    With a live :class:`HudLauncher` instance: return :class:`HudNotifier`.
    Otherwise (tests, ``SAYZO_NOTIFICATIONS_ENABLED=0``, or an unrecoverable
    HUD startup failure): return :class:`NoopNotifier`.
    """
    if launcher is None:
        return NoopNotifier()
    return HudNotifier(launcher)
