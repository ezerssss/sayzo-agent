"""NSUserNotification-based notifier for unsigned macOS bundles (dev fallback).

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

When this module gets used (and when it doesn't)
------------------------------------------------

Production releases are Developer-ID-signed and Apple-notarized in CI
(see ``.github/workflows/build.yml``), so ``is_signed_bundle()`` returns
True and ``notify.make_notifier`` falls through to the modern
``DesktopNotifier`` / ``UNUserNotificationCenter`` path — this module
is dead code on shipped builds. It's exercised when a developer runs
the agent from source, or builds a local PyInstaller bundle without
the CI signing steps. Keeping it lets dev workflows still get visible
toasts without forcing every contributor to set up an Apple Developer
account.

Trade-offs
----------

* **Deprecation risk** — Apple has telegraphed ``NSUserNotification``
  removal for years; macOS 15 still has it. A future macOS may drop it.
  Production builds avoid this risk entirely by going through the
  modern ``UNUserNotificationCenter`` backend.
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
from typing import Any, Callable, Literal, Optional

log = logging.getLogger(__name__)


ConsentResult = Literal["yes", "no", "timeout"]


# NSUserNotification activation type constants (NSUserNotificationActivationType
# enum from <Foundation/NSUserNotification.h>). We don't pull these from
# pyobjc at module load because the import would fail on non-mac CI; the
# coroutine that needs them imports inside its try-block.
_NSUNOT_ACTIVATION_NONE = 0
_NSUNOT_ACTIVATION_CONTENTS_CLICKED = 1
_NSUNOT_ACTIVATION_ACTION_BUTTON_CLICKED = 2
_NSUNOT_ACTIVATION_REPLIED = 3
_NSUNOT_ACTIVATION_ADDITIONAL_ACTION_CLICKED = 4


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

        # actionable-toast click dispatch: keyed by NSUserNotification
        # identifier. Kept as a dict so the delegate (which receives the
        # raw NSUserNotification on click) can route back to Python.
        # Strong reference to the delegate prevents pyobjc-default GC.
        self._action_callbacks: dict[str, Callable[[], None]] = {}
        self._delegate_ref: Any = None

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

        # Install the NSUserNotificationCenterDelegate once. The delegate
        # routes activation events (button click, content click) back to
        # the per-notification callbacks registered via notify_actionable.
        try:
            self._install_delegate()
        except Exception:
            log.warning(
                "[notify-mac-legacy] failed to install NSUserNotificationCenter "
                "delegate; actionable toasts will not round-trip clicks",
                exc_info=True,
            )

        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def _install_delegate(self) -> None:
        """Set ourselves as the NSUserNotificationCenter delegate.

        The delegate's ``userNotificationCenter:didActivateNotification:``
        method fires when the user clicks the toast body, the action
        button, or the close button. We dispatch on ``activationType``
        to the registered ``on_pressed`` for that notification's
        identifier. pyobjc subclasses ``NSObject`` declaratively here.
        """
        from Foundation import (  # type: ignore[import-not-found]
            NSObject,
            NSUserNotificationCenter,
        )
        import objc  # type: ignore[import-not-found]

        notifier = self  # captured for closure use inside the ObjC class

        class _SayzoNotifyDelegate(NSObject):
            # pyobjc selector signature: voidv@:@@ (void method taking two
            # object args). Method names follow Cocoa's naming convention.
            def userNotificationCenter_didActivateNotification_(
                self_, center, notification
            ):
                try:
                    activation = int(notification.activationType())
                except Exception:
                    activation = _NSUNOT_ACTIVATION_NONE
                ident = None
                try:
                    ident = str(notification.identifier())
                except Exception:
                    pass
                log.info(
                    "[notify-mac-legacy] delegate activation: id=%s type=%d",
                    ident,
                    activation,
                )
                if activation not in (
                    _NSUNOT_ACTIVATION_ACTION_BUTTON_CLICKED,
                    _NSUNOT_ACTIVATION_CONTENTS_CLICKED,
                ):
                    return
                if not ident:
                    return
                cb = notifier._action_callbacks.pop(ident, None)
                if cb is None:
                    return
                try:
                    cb()
                except Exception:
                    log.warning(
                        "[notify-mac-legacy] delegate callback raised: id=%s",
                        ident,
                        exc_info=True,
                    )

            # Always present even when the OS thinks the user has disabled
            # us (e.g., banner style = none) — without this, clicks from
            # Notification Center don't always route back to us.
            def userNotificationCenter_shouldPresentNotification_(
                self_, center, notification
            ):
                return True

        delegate = _SayzoNotifyDelegate.alloc().init()
        self._delegate_ref = delegate  # strong ref — pyobjc weak-refs by default
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.setDelegate_(delegate)
        log.info("[notify-mac-legacy] NSUserNotificationCenter delegate installed")

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

    def notify_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Fire-and-forget actionable toast with a single action button.

        On NSUserNotification the action button is the right-hand button;
        clicking the toast body also activates (we treat both as engagement).
        See ``_install_delegate`` for the activation-type dispatch.

        Returns ``True`` if dispatched. ``on_pressed`` and ``on_expire``
        are mutually exclusive: a single-fire latch means whichever
        happens first wins. Both callbacks run on the notifier loop.
        """
        if self._init_failed or self._loop is None:
            log.warning(
                "[notify-mac-legacy] notify_actionable dropped (backend "
                "unavailable): title=%r",
                title,
            )
            return False
        log.info(
            "[notify-mac-legacy] actionable scheduled: title=%r button=%r "
            "expire_after=%ss",
            title,
            button_label,
            expire_after_secs,
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self._async_deliver_actionable(
                    title, body, button_label, on_pressed, expire_after_secs, on_expire,
                ),
                self._loop,
            )
            return True
        except Exception:
            log.warning(
                "[notify-mac-legacy] actionable schedule failed", exc_info=True
            )
            return False

    def has_authorisation_sync(self) -> Optional[bool]:
        """NSUserNotification has no auth concept — always True if the
        backend booted at all."""
        if self._init_failed or self._loop is None:
            return None
        return True

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

    async def _async_deliver_actionable(
        self,
        title: str,
        body: str,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]],
    ) -> None:
        """Loop-local: deliver the toast + register click + watchdog."""
        loop = asyncio.get_running_loop()
        latch = {"fired": False}
        timer_handle: dict[str, Any] = {"h": None}

        def _fire_pressed() -> None:
            if latch["fired"]:
                return
            latch["fired"] = True
            log.info("[notify-mac-legacy] actionable pressed: title=%r", title)
            handle = timer_handle.get("h")
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:
                    pass
            try:
                on_pressed()
            except Exception:
                log.warning(
                    "[notify-mac-legacy] actionable on_pressed raised",
                    exc_info=True,
                )

        def _fire_expire() -> None:
            if latch["fired"]:
                return
            latch["fired"] = True
            log.info("[notify-mac-legacy] actionable expired: title=%r", title)
            # Drop the entry from the dispatch table so a late click
            # (Notification Center can hold the toast for hours) finds
            # nothing to call.
            self._action_callbacks.pop(_pending_ident["id"], None)
            if on_expire is None:
                return
            try:
                on_expire()
            except Exception:
                log.warning(
                    "[notify-mac-legacy] actionable on_expire raised",
                    exc_info=True,
                )

        # Register the dispatch entry BEFORE delivering so a fast click
        # arriving before this coroutine resumes still finds the callback.
        # The delegate hops back to the notifier loop via threadsafe
        # scheduling so the latch + timer-cancel run consistently.
        def _delegate_invoked() -> None:
            try:
                loop.call_soon_threadsafe(_fire_pressed)
            except RuntimeError:
                pass

        ident = await self._async_deliver(
            title, body, action_title=button_label, on_press=_delegate_invoked,
        )
        _pending_ident: dict[str, Optional[str]] = {"id": ident}
        if ident is None:
            # Delivery failed — fire the expire callback so the scheduler
            # can record the outcome and move on.
            _fire_expire()
            return

        timer_handle["h"] = loop.call_later(expire_after_secs, _fire_expire)

    async def _async_deliver(
        self,
        title: str,
        body: str,
        *,
        action_title: Optional[str],
        on_press: Optional[Callable[[], None]] = None,
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
                if on_press is not None:
                    # Register click dispatch BEFORE delivery so a very
                    # fast click can't arrive at the delegate before the
                    # entry exists.
                    self._action_callbacks[ident] = on_press
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

