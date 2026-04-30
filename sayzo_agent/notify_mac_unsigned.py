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
    same ``notify`` / ``ask_consent`` / ``diagnose`` surface, same
    threading contract (own asyncio loop on a daemon thread, callbacks
    bridged onto it). Auto-selected by ``notify.make_notifier`` when
    ``is_signed_bundle()`` returns False.

    All exceptions are swallowed and logged — toasts are auxiliary UX,
    they never bring down the capture pipeline.
    """

    def __init__(self, app_name: str = "Sayzo") -> None:
        self._app_name = app_name
        self._init_failed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        # Pinned reference to the ObjC delegate. PyObjC will GC the
        # Python wrapper otherwise, dropping the methods the notification
        # center is supposed to call back into.
        self._delegate = None
        # identifier → asyncio.Future. The delegate (called on the main
        # thread by the notification center) finds the pending future
        # by identifier and resolves it on the notifier's loop. Lock
        # protects the dict against simultaneous access from the
        # delegate thread + the notifier thread.
        self._pending: dict[str, asyncio.Future[ConsentResult]] = {}
        self._pending_lock = threading.Lock()
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

        try:
            self._install_delegate()
        except Exception:
            log.warning(
                "[notify-mac-legacy] delegate install failed; consent toasts "
                "will fall through to timeout",
                exc_info=True,
            )
            # Don't mark init_failed — fire-and-forget notify() still works
            # without a delegate. ask_consent will just always return
            # "timeout" which the caller maps to default_on_timeout.

        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def _install_delegate(self) -> None:
        """Subclass NSObject to receive notification activation callbacks.

        Calls ``[NSUserNotificationCenter setDelegate:]`` so the system
        invokes our methods when the user clicks the notification body
        or its action button. The delegate methods fire on the main
        thread (where pystray's NSApp run loop is pumping the
        CFRunLoop), and we marshal the resolution onto the notifier's
        own asyncio loop via ``call_soon_threadsafe``.
        """
        from Foundation import NSObject, NSUserNotificationCenter  # type: ignore[import-not-found]

        wrapper = self  # captured by the inner class

        class _SayzoLegacyDelegate(NSObject):  # type: ignore[misc]
            # The two delegate selectors NSUserNotificationCenter knows
            # about. PyObjC matches by selector name; both must exist
            # even if shouldPresent always returns True.

            def userNotificationCenter_didActivateNotification_(
                self, center, notification
            ) -> None:
                try:
                    ident = (
                        str(notification.identifier())
                        if notification.identifier() is not None
                        else ""
                    )
                    activation_type = int(notification.activationType())
                    # 1 = ContentsClicked, 2 = ActionButtonClicked,
                    # 3 = Replied (we don't use), 4 = AdditionalActionClicked
                    yes = activation_type in (1, 2, 4)
                    log.info(
                        "[notify-mac-legacy] delegate fired: id=%s "
                        "activation_type=%s → %s",
                        ident,
                        activation_type,
                        "yes" if yes else "no",
                    )
                    wrapper._resolve(ident, "yes" if yes else "no")
                except Exception:
                    log.warning(
                        "[notify-mac-legacy] delegate handler failed",
                        exc_info=True,
                    )

            def userNotificationCenter_shouldPresentNotification_(
                self, center, notification
            ) -> bool:
                # Always present — Sayzo is a background process with
                # no foreground window concept, so the default
                # "suppress while frontmost" behavior would silently
                # eat every toast we send during onboarding.
                return True

        delegate = _SayzoLegacyDelegate.alloc().init()
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.setDelegate_(delegate)
        # Pin so PyObjC's GC doesn't drop the wrapper while the notification
        # center is still calling into it.
        self._delegate = delegate
        log.info("[notify-mac-legacy] delegate installed on default center")

    def _resolve(self, identifier: str, value: ConsentResult) -> None:
        """Bridge a delegate-thread click into the notifier loop."""
        with self._pending_lock:
            fut = self._pending.pop(identifier, None)
        if fut is None or fut.done():
            return
        loop = fut.get_loop()
        try:
            loop.call_soon_threadsafe(_set_future_result, fut, value)
        except RuntimeError:
            # Loop closed mid-shutdown.
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
        """Interactive toast with a single Yes button via NSUserNotification.

        ``no_label`` is unused — only one button reliably renders on
        Banner mode. The whole consent flow collapses to "did the user
        click Yes within ``timeout_secs``?"; anything else (dismiss,
        banner timeout, app quit) returns ``"timeout"``. The
        ArmController already treats timeout as no-arm in every consent
        site, so this is a behaviour-preserving simplification — the
        non-yes paths weren't meaningfully distinguishable on macOS
        even with the modern API.
        """
        if self._init_failed or self._loop is None:
            log.warning(
                "[notify-mac-legacy] ask_consent dropped (backend down): "
                "title=%r",
                title,
            )
            return default_on_timeout
        log.info(
            "[notify-mac-legacy] ask scheduled: title=%r yes=%r timeout=%ss",
            title,
            yes_label,
            timeout_secs,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._async_consent(title, body, yes_label, timeout_secs),
                self._loop,
            )
            result = fut.result(timeout=timeout_secs + 5.0)
            log.info(
                "[notify-mac-legacy] ask resolved: title=%r → %s", title, result
            )
            return result
        except Exception:
            log.warning(
                "[notify-mac-legacy] ask_consent failed: title=%r",
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
            consent_result = asyncio.run_coroutine_threadsafe(
                self._async_consent(
                    "Sayzo consent test",
                    "Click Yes within 3 seconds to confirm consent toasts work.",
                    "Yes",
                    3.0,
                ),
                self._loop,
            ).result(timeout=_DELIVER_TIMEOUT_SECS + 4)
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

    async def _async_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        timeout_secs: float,
    ) -> ConsentResult:
        loop = asyncio.get_running_loop()
        result_fut: asyncio.Future[ConsentResult] = loop.create_future()
        ident = await self._async_deliver(title, body, action_title=yes_label)
        if ident is None:
            return "timeout"

        with self._pending_lock:
            self._pending[ident] = result_fut

        try:
            return await asyncio.wait_for(result_fut, timeout=timeout_secs)
        except asyncio.TimeoutError:
            with self._pending_lock:
                self._pending.pop(ident, None)
            log.info(
                "[notify-mac-legacy] consent timed out: title=%r id=%s",
                title,
                ident,
            )
            return "timeout"


def _set_future_result(
    fut: asyncio.Future[ConsentResult], value: ConsentResult
) -> None:
    """call_soon_threadsafe target — guard against double-resolve."""
    if not fut.done():
        fut.set_result(value)
