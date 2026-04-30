"""NSUserNotification-based notifier for unsigned macOS bundles.

Why this exists
---------------

Modern ``UNUserNotificationCenter`` (used by ``desktop-notifier`` 6.x)
silently drops notifications from unsigned app bundles. Every API call
succeeds — ``addNotificationRequest:withCompletionHandler:`` fires with
``error=nil``, ``has_authorisation`` returns True, the OS even shows
"Sayzo" in System Settings → Notifications — but the notification
daemon checks the bundle signature and never actually displays the toast.

``NSUserNotification`` (deprecated since macOS 11.0, still functional
on macOS 15) is less strict and works on unsigned bundles as long as
the ``Info.plist`` carries a ``CFBundleIdentifier``. Ours does
(``com.sayzo.agent`` set in ``sayzo-agent.spec``), so we can route
toasts through this older API and they actually appear.

This is a stop-gap. The proper fix is signing + notarisation: populate
``APPLE_DEVELOPER_ID`` and the notarytool secrets in CI and the
existing build pipeline (``.github/workflows/build.yml``) takes care
of the rest. With a signed bundle, ``DesktopNotifier`` in
``notify.py`` Just Works and this module is no-op'd.

Trade-offs
----------

* **Deprecation risk** — Apple has telegraphed ``NSUserNotification``
  removal for years; macOS 15 still has it. A future macOS may drop it.
  Sign sooner rather than later.
* **One button on consent toasts** — only the action button renders
  reliably across Banner / Alert styles. ``ask_consent`` therefore
  shows a single "Yes" button; user dismiss / banner timeout / Other
  button click all collapse to ``"timeout"`` (which the ArmController
  treats as no-arm). For our consent UX this is fine — recording is
  opt-in by definition.
* **No reply field** — we don't use this anyway.
* **Identity** — toasts appear as "Sayzo" because we own
  ``CFBundleIdentifier`` and the icon ships in the bundle. macOS picks
  these up automatically once Launch Services has indexed the .app
  (which happens on first DMG-install of the bundle).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import uuid
from typing import Any, Literal, Optional

log = logging.getLogger(__name__)


ConsentResult = Literal["yes", "no", "timeout"]


# Mirror the wait budget in ``notify.py`` so identical "this hung"
# log messages cap out at the same wall-clock interval and the user
# doesn't have to reason about two separate timeouts.
_DELIVER_TIMEOUT_SECS = 8.0


class MacUnsignedNotifier:
    """Notifier that works on unsigned macOS bundles via ``NSUserNotification``.

    Drop-in replacement for :class:`sayzo_agent.notify.DesktopNotifier` —
    same ``notify`` / ``ask_consent`` / ``diagnose`` surface. Fire-and-
    forget toasts go through ``NSUserNotification`` on the notifier's
    own asyncio loop (daemon thread). Consent dialogs route through
    :func:`sayzo_agent.consent_modal.consent_modal_macos` (osascript)
    — the same path the signed-bundle notifier uses, since the modal
    is independent of bundle signature anyway. Auto-selected by
    ``notify.make_notifier`` when ``is_signed_bundle()`` returns False.

    All exceptions are swallowed and logged — toasts are auxiliary UX,
    they never bring down the capture pipeline.
    """

    def __init__(self, app_name: str = "Sayzo") -> None:
        self._app_name = app_name
        self._init_failed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._bundle_info: dict[str, Any] = {}

        log.info(
            "[notify-mac-legacy] init: app_name=%r platform=darwin frozen=%s "
            "(NSUserNotification fallback for unsigned bundle)",
            app_name,
            getattr(sys, "frozen", False),
        )

        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"{app_name}-notifier-mac-legacy",
            daemon=True,
        )
        self._thread.start()
        if not self._loop_ready.wait(timeout=5.0):
            log.warning(
                "[notify-mac-legacy] init: loop did not become ready in 5s"
            )

    # ------------------------------------------------------------------
    # Loop thread
    # ------------------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except Exception:
            self._init_failed = True
            self._loop_ready.set()
            log.warning(
                "[notify-mac-legacy] event loop init failed", exc_info=True
            )
            return

        try:
            from .notify import _capture_macos_bundle_info  # type: ignore[attr-defined]

            self._bundle_info = _capture_macos_bundle_info()
            log.info(
                "[notify-mac-legacy] bundle: is_bundle=%s is_signed=%s id=%s "
                "macos_version=%s path=%s",
                self._bundle_info.get("is_bundle"),
                self._bundle_info.get("is_signed"),
                self._bundle_info.get("bundle_id"),
                self._bundle_info.get("macos_version"),
                self._bundle_info.get("bundle_path"),
            )
        except Exception:
            log.debug(
                "[notify-mac-legacy] bundle introspection failed", exc_info=True
            )

        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(self, title: str, body: str) -> None:
        """Fire-and-forget toast via NSUserNotification."""
        if self._init_failed or self._loop is None:
            log.warning(
                "[notify-mac-legacy] notify dropped (backend unavailable): "
                "title=%r",
                title,
            )
            return
        log.info("[notify-mac-legacy] notify scheduled: title=%r", title)
        try:
            asyncio.run_coroutine_threadsafe(
                self._async_deliver(title, body, action_title=None),
                self._loop,
            )
        except Exception:
            log.warning(
                "[notify-mac-legacy] schedule failed: title=%r",
                title,
                exc_info=True,
            )

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
    ) -> ConsentResult:
        """Modal dialog via :mod:`sayzo_agent.consent_modal`.

        Notifications-with-buttons on the legacy NSUserNotification
        API render only one button reliably (the Action button), and
        on Banner-style preference of "None" they silently route to
        Notification Center. For consent decisions the user must see,
        a modal is the correct tool — same as the signed path in
        ``notify.DesktopNotifier.ask_consent``.
        """
        log.info(
            "[notify-mac-legacy] ask scheduled (modal): title=%r yes=%r no=%r timeout=%ss",
            title,
            yes_label,
            no_label,
            timeout_secs,
        )
        try:
            from .consent_modal import consent_modal_macos

            result = consent_modal_macos(
                title,
                body,
                yes_label,
                no_label,
                timeout_secs,
                default_on_timeout,
            )
            log.info(
                "[notify-mac-legacy] ask resolved (modal): title=%r → %s",
                title,
                result,
            )
            return result
        except Exception:
            log.warning(
                "[notify-mac-legacy] modal ask_consent failed: title=%r",
                title,
                exc_info=True,
            )
            return default_on_timeout

    def diagnose(self) -> dict[str, Any]:
        """Mirror ``DesktopNotifier.diagnose`` so the CLI report shape is
        identical regardless of which backend is selected."""
        report: dict[str, Any] = {
            "platform": sys.platform,
            "frozen": getattr(sys, "frozen", False),
            "app_name": self._app_name,
            "init_failed": self._init_failed,
            "backend": "MacUnsignedNotifier (NSUserNotification)",
            "bundle": dict(self._bundle_info),
            "has_authorisation": True,  # NSUserNotification has no auth concept
            "test_send": None,
            "consent_send": None,
            "errors": [],
        }
        if self._init_failed or self._loop is None:
            report["errors"].append("backend unavailable")
            return report

        try:
            ident = asyncio.run_coroutine_threadsafe(
                self._async_deliver(
                    "Sayzo notification test",
                    "If you can read this, the unsigned-bundle fallback works.",
                    action_title=None,
                ),
                self._loop,
            ).result(timeout=_DELIVER_TIMEOUT_SECS + 2)
            report["test_send"] = {"ok": True, "id": ident}
            log.info("[notify-mac-legacy] diagnose: test_send id=%s", ident)
        except Exception as exc:
            report["test_send"] = {"ok": False, "error": repr(exc)}
            log.warning(
                "[notify-mac-legacy] diagnose: test_send failed", exc_info=True
            )

        try:
            from .consent_modal import consent_modal_macos

            consent_result = consent_modal_macos(
                "Sayzo consent test",
                "Click Yes within 5 seconds to confirm consent dialogs work.",
                "Yes",
                "No",
                5.0,
            )
            report["consent_send"] = {"ok": True, "result": consent_result}
            log.info(
                "[notify-mac-legacy] diagnose: consent_send result=%s",
                consent_result,
            )
        except Exception as exc:
            report["consent_send"] = {"ok": False, "error": repr(exc)}
            log.warning(
                "[notify-mac-legacy] diagnose: consent_send failed",
                exc_info=True,
            )

        return report

    # ------------------------------------------------------------------
    # Loop-local coroutines
    # ------------------------------------------------------------------

    async def _async_deliver(
        self,
        title: str,
        body: str,
        *,
        action_title: Optional[str],
    ) -> Optional[str]:
        """Build + deliver an NSUserNotification. Returns its identifier
        on success or ``None`` on failure. Runs on the notifier loop;
        delivery itself is thread-safe per Apple but we keep all ObjC
        calls on this same thread for predictability.
        """
        try:
            from Foundation import (  # type: ignore[import-not-found]
                NSUserNotification,
                NSUserNotificationCenter,
            )
        except Exception:
            log.warning(
                "[notify-mac-legacy] PyObjC Foundation unavailable",
                exc_info=True,
            )
            return None

        try:
            ident = uuid.uuid4().hex
            notif = NSUserNotification.alloc().init()
            notif.setIdentifier_(ident)
            notif.setTitle_(title)
            notif.setInformativeText_(body)
            if action_title is not None:
                # When ``hasActionButton`` is True macOS reserves the
                # right-hand slot for our button and omits the default
                # close-X glyph; for fire-and-forget toasts we leave
                # ``hasActionButton`` False so the toast auto-dismisses
                # without offering a click target.
                notif.setHasActionButton_(True)
                notif.setActionButtonTitle_(action_title)
            else:
                notif.setHasActionButton_(False)

            center = NSUserNotificationCenter.defaultUserNotificationCenter()
            log.info(
                "[notify-mac-legacy] deliver begin: title=%r id=%s "
                "action=%r",
                title,
                ident,
                action_title,
            )
            center.deliverNotification_(notif)
            log.info(
                "[notify-mac-legacy] deliver done: title=%r id=%s",
                title,
                ident,
            )
            return ident
        except Exception:
            log.warning(
                "[notify-mac-legacy] deliver failed: title=%r",
                title,
                exc_info=True,
            )
            return None

