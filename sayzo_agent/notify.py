"""Cross-platform desktop notifications for conversation events.

Failures are always swallowed and logged — a broken toast backend must never
bring down the main capture pipeline.

In the armed-only model (v1.0+) this module has two jobs:

1. **Fire-and-forget** via ``notify(title, body)`` — same semantics as
   before (capture-saved, post-arm guidance, stream-open error, etc.).
2. **Interactive consent** via ``ask_consent(...)`` — toast with two action
   buttons, await the user's click (or timeout), return ``"yes"``, ``"no"``,
   or ``"timeout"``. Used by the ArmController for whitelist consent, hotkey
   start/stop confirmation, end-of-meeting confirmation, long-meeting
   check-in, and meeting-ended-watcher toasts.

Interactive buttons require ``desktop-notifier``'s async API. The sync
wrapper we used to rely on is deprecated and doesn't round-trip button
callbacks reliably on Windows. Instead we spin up a dedicated asyncio loop
on a background daemon thread (created eagerly in ``__init__`` so the first
consent toast isn't gated on loop startup), and marshal all work onto it
via ``asyncio.run_coroutine_threadsafe``.

Diagnostic logging
------------------

When users report "no toasts appear," the failure is almost always in one
of three layers: our wrapper here, ``desktop-notifier`` itself, or the
OS. To make field debugging tractable we log at every step:

* ``[notify] init`` — module-level state at notifier construction
  (app_name, platform, frozen flag, icon path).
* ``[notify] backend init`` — when the underlying ``desktop_notifier``
  backend is constructed, plus on macOS a ``[notify] bundle`` line with
  ``is_bundle`` / ``is_signed`` / ``CFBundleIdentifier`` / ``bundlePath``.
* ``[notify] auth probe`` — initial ``has_authorisation()`` result, so
  we can tell at a glance whether the OS is granting us toast rights
  even before the first user-visible send.
* ``[notify] notify scheduled`` / ``[notify] send begin`` /
  ``[notify] send done id=…`` — full path on every notify call. A
  missing ``send done`` means the OS completion handler never fired
  (CFRunLoop/main-thread issue or XPC daemon hang); a thrown exception
  surfaces as ``[notify] send failed`` with the traceback.

The same shape applies to ``ask_consent`` (``[notify] ask scheduled``,
``[notify] ask send done``, ``[notify] ask resolved=…``). Run
``sayzo-agent diagnose-notifications`` to exercise the full path with
explicit reporting; the same lines also flow into ``agent.log`` during
normal operation so a user's existing log file is enough to debug a
silent-toast report.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol

log = logging.getLogger(__name__)


ConsentResult = Literal["yes", "no", "timeout"]


# AUMID that every Sayzo notifier instance must be constructed with. Must
# match the Start Menu shortcut AppUserModelID set by the NSIS installer
# (see ``installer/windows/sayzo-agent.nsi``) — otherwise WinRT toasts
# silently fail to render on Windows 10. On macOS this is the display name
# attributed to the notification. Any drift is a silent regression.
APP_AUMID = "Sayzo"

# Hard ceiling on how long we wait for the OS to dispatch a notification
# before giving up. ``addNotificationRequest:withCompletionHandler:`` (macOS)
# / ``ToastNotifier.Show()`` (Windows WinRT) should complete in well under a
# second; if we hit this timeout it almost always means the completion
# handler never fired — typically because the host app's main-thread
# CFRunLoop / message pump isn't running, or because the OS has silently
# refused the notification. Logging the timeout makes that failure mode
# visible in agent.log instead of hanging the consent / heartbeat path
# forever.
_SEND_TIMEOUT_SECS = 8.0


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...

    def notify_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
    ) -> bool: ...

    def has_authorisation_sync(self) -> Optional[bool]: ...


class NoopNotifier:
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
        log.debug("[notify] (noop) consent %s — %s → %s", title, body, default_on_timeout)
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
        log.debug(
            "[notify] (noop) actionable %s — %s [%s]", title, body, button_label
        )
        # Test paths can drive the expire branch by registering on_expire.
        if on_expire is not None:
            try:
                on_expire()
            except Exception:
                log.debug("[notify] (noop) on_expire raised", exc_info=True)
        return False

    def has_authorisation_sync(self) -> Optional[bool]:
        return None


def _logo_path() -> Path:
    """Resolve the Sayzo logo bundled alongside the tray icon.

    Mirrors ``sayzo_agent/gui/tray.py::_logo_path`` so dev and PyInstaller-
    frozen builds both land on the same asset.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "installer" / "assets"  # type: ignore[attr-defined]
    else:
        # notify.py is sayzo_agent/notify.py — parent.parent is repo root.
        base = Path(__file__).resolve().parent.parent / "installer" / "assets"
    return base / "logo.png"


def _capture_macos_bundle_info() -> dict[str, Any]:
    """Best-effort introspection of NSBundle + codesign state on macOS.

    Returns a dict that's safe to log — every field is either a string,
    bool, or ``None`` if the lookup failed. Called at notifier init so
    we can correlate "no toast" reports against a known-good bundle
    identity. Mirrors the same checks ``desktop-notifier`` itself uses
    to decide whether to instantiate the real UNN backend or fall back
    to its dummy one.
    """
    info: dict[str, Any] = {
        "is_bundle": None,
        "is_signed": None,
        "bundle_id": None,
        "bundle_path": None,
        "executable_path": None,
        "macos_version": None,
    }
    try:
        from desktop_notifier.backends.macos_support import (  # type: ignore[import-not-found]
            is_bundle,
            is_signed_bundle,
            macos_version,
        )

        info["is_bundle"] = bool(is_bundle())
        info["is_signed"] = bool(is_signed_bundle())
        info["macos_version"] = str(macos_version)
    except Exception:
        log.warning(
            "[notify] bundle introspection: desktop-notifier shim import failed",
            exc_info=True,
        )

    try:
        from AppKit import NSBundle  # type: ignore[import-not-found]

        main = NSBundle.mainBundle()
        if main is not None:
            try:
                info["bundle_id"] = str(main.bundleIdentifier()) if main.bundleIdentifier() else None
            except Exception:
                pass
            try:
                info["bundle_path"] = str(main.bundlePath()) if main.bundlePath() else None
            except Exception:
                pass
            try:
                info["executable_path"] = (
                    str(main.executablePath()) if main.executablePath() else None
                )
            except Exception:
                pass
    except Exception:
        log.warning("[notify] bundle introspection: AppKit unavailable", exc_info=True)

    return info


class DesktopNotifier:
    """Native toast via the `desktop-notifier` PyPI package, async backend.

    Owns a dedicated asyncio loop running on a daemon background thread.
    Both ``notify`` and ``ask_consent`` are thread-safe — they marshal onto
    the loop via ``asyncio.run_coroutine_threadsafe``.

    The backend is constructed eagerly in ``__init__`` (on the background
    thread, since Windows WinRT pins COM to the constructing thread). If
    backend init fails the notifier degrades to a noop — exceptions are
    logged, never propagated.

    ``app_name`` must match the AUMID set on the Start Menu shortcut by the
    NSIS installer ("Sayzo") for WinRT toasts to appear at all on Windows 10;
    on macOS it's the display name attributed to the notification.
    Interactive button callbacks work on both WinRT (Windows 10+) and
    NSUserNotification (macOS) back-ends.
    """

    def __init__(self, app_name: str = "Sayzo") -> None:
        self._app_name = app_name
        self._impl = None  # desktop_notifier.DesktopNotifier instance
        self._init_failed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        # Filled during _thread_main on macOS for diagnostic logging.
        # Stored on the instance so ``diagnose-notifications`` CLI / future
        # GUI surfaces can read it back without re-doing the introspection.
        self._bundle_info: dict[str, Any] = {}

        log.info(
            "[notify] init: app_name=%r platform=%s frozen=%s",
            app_name,
            sys.platform,
            getattr(sys, "frozen", False),
        )

        # On Windows the desktop-notifier backend activates winrt notification
        # APIs, which load Windows Runtime DLLs that subsequently break torch's
        # own DLL initialization (c10.dll) — any later `import torch` via
        # silero_vad dies with WinError 1114. Preloading torch first pins its
        # DLLs so the winrt load can't clobber them. The PyInstaller bundle
        # sidesteps this by shipping DLLs next to the exe; dev installs don't.
        if sys.platform == "win32":
            try:
                import torch  # noqa: F401
            except Exception:
                pass

        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"{app_name}-notifier",
            daemon=True,
        )
        self._thread.start()
        # Block briefly for the loop to come up so the first caller doesn't
        # race the loop creation. If the thread errors out before ready, the
        # wait times out and later calls no-op.
        if not self._loop_ready.wait(timeout=5.0):
            log.warning("[notify] init: loop did not become ready within 5s")

    # ---- loop thread -------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except Exception:
            self._init_failed = True
            self._loop_ready.set()
            log.warning("[notify] event loop init failed; toasts disabled", exc_info=True)
            return

        try:
            from desktop_notifier import DesktopNotifier as _Async, Icon

            icon_path = _logo_path()
            icon_exists = icon_path.exists()
            log.info(
                "[notify] icon: path=%s exists=%s", icon_path, icon_exists
            )
            app_icon = Icon(path=icon_path) if icon_exists else None

            # Surface desktop-notifier's own logs into agent.log. The package
            # logs warnings at codesign-check failures, debug at each send,
            # info on auth grant — all useful when triaging a silent-toast
            # report. Without this its logger is at WARNING by default and
            # the warnings DO flow through the root file handler, but we
            # bump to INFO so the codesign verdict is visible without
            # needing a separate debug build.
            try:
                logging.getLogger("desktop_notifier").setLevel(logging.INFO)
            except Exception:
                pass

            self._impl = _Async(app_name=self._app_name, app_icon=app_icon)
            log.info(
                "[notify] backend init OK: %s",
                type(self._impl)._backend_class().__name__
                if hasattr(type(self._impl), "_backend_class")
                else type(getattr(self._impl, "_backend", self._impl)).__name__,
            )
        except Exception:
            self._init_failed = True
            log.warning(
                "[notify] backend init failed; toasts disabled", exc_info=True
            )

        # macOS-only: log NSBundle identity + codesign verdict so a
        # user's agent.log immediately tells us whether UNN is going to
        # accept anything we send. On a production release this should
        # always read ``is_signed=True`` (CI signs + notarizes). Seeing
        # ``is_signed=False`` here on a shipped build means something
        # mangled the signature post-install — typically a manager-Mac
        # quarantine policy or a stale TCC entry from a prior unsigned
        # build; on a dev source-run it just means no signing happened.
        if sys.platform == "darwin" and not self._init_failed:
            try:
                self._bundle_info = _capture_macos_bundle_info()
                log.info(
                    "[notify] bundle: is_bundle=%s is_signed=%s id=%s "
                    "macos_version=%s path=%s exec=%s",
                    self._bundle_info.get("is_bundle"),
                    self._bundle_info.get("is_signed"),
                    self._bundle_info.get("bundle_id"),
                    self._bundle_info.get("macos_version"),
                    self._bundle_info.get("bundle_path"),
                    self._bundle_info.get("executable_path"),
                )
            except Exception:
                log.warning(
                    "[notify] bundle introspection failed", exc_info=True
                )

        # Initial authorisation probe. We schedule it on the loop right
        # before we hand the thread over to ``run_forever`` — otherwise
        # the coroutine never runs. A False here when System Settings
        # claims the toggle is on means TCC is silently denying us.
        if not self._init_failed and self._impl is not None:
            try:
                # Schedule but don't wait — the loop is about to start.
                asyncio.run_coroutine_threadsafe(self._log_initial_auth(), loop)
            except Exception:
                log.debug(
                    "[notify] could not schedule initial auth probe",
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

    async def _log_initial_auth(self) -> None:
        """Probe ``has_authorisation`` once at boot and log the result.

        Runs on the notifier's asyncio loop. Time-bounded so a hung
        completion handler doesn't sit forever; the timeout itself is
        information ("[notify] auth probe timed out" → main thread
        CFRunLoop almost certainly isn't pumping yet).
        """
        if self._impl is None:
            return
        try:
            result = await asyncio.wait_for(
                self._impl_has_authorisation(), timeout=_SEND_TIMEOUT_SECS
            )
            log.info("[notify] auth probe: has_authorisation=%s", result)
        except asyncio.TimeoutError:
            log.warning(
                "[notify] auth probe timed out after %ss — completion "
                "handler did not fire (main-thread run loop not pumping?)",
                _SEND_TIMEOUT_SECS,
            )
        except Exception:
            log.warning("[notify] auth probe failed", exc_info=True)

    async def _impl_has_authorisation(self) -> Optional[bool]:
        """Wrap ``DesktopNotifier.has_authorisation`` so callers don't
        need to know whether the backend exposes it. Returns None if
        the backend has no such API (e.g. dummy fallback)."""
        if self._impl is None:
            return None
        method = getattr(self._impl, "has_authorisation", None)
        if method is None:
            return None
        return bool(await method())

    # ---- public API --------------------------------------------------------

    def notify(self, title: str, body: str) -> None:
        """Fire-and-forget toast. Thread-safe."""
        if self._init_failed or self._impl is None or self._loop is None:
            log.warning(
                "[notify] notify dropped (backend unavailable): title=%r", title
            )
            return
        log.info("[notify] notify scheduled: title=%r", title)
        try:
            asyncio.run_coroutine_threadsafe(
                self._send(title, body), self._loop
            )
        except Exception:
            log.warning("[notify] schedule failed", exc_info=True)

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
    ) -> ConsentResult:
        """Ask the user a yes/no question that needs to be seen.

        On **macOS** this dispatches to a modal dialog (``osascript
        display dialog`` via :mod:`sayzo_agent.consent_modal`) rather
        than a notification — for **visibility**. macOS notifications
        get silently suppressed under Banner Style: None or Focus
        mode (they land in Notification Center where the user has to
        click the menu-bar clock to find them); a consent prompt that
        the user might not see is worse than no prompt at all. Modals
        are a window, not a notification, so Banner Style + Focus
        don't apply — and as a side benefit they're independent of
        bundle signing / notification permissions, which keeps dev
        builds working too.

        On **Windows** we keep using the notification path — WinRT
        toasts with action buttons are reliable enough that a modal
        is unnecessary. The ``ask_consent`` contract is otherwise
        identical across platforms.

        Returns ``"yes"`` / ``"no"`` / ``"timeout"``. ``timeout`` is
        passed through distinctly so callers can distinguish "clicked
        No" from "ignored". On any failure we return
        ``default_on_timeout`` — a broken consent path must never
        crash the capture pipeline.
        """
        if sys.platform == "darwin":
            log.info(
                "[notify] ask scheduled (modal): title=%r yes=%r no=%r timeout=%ss",
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
                log.info("[notify] ask resolved (modal): title=%r → %s", title, result)
                return result
            except Exception:
                log.warning(
                    "[notify] modal ask_consent failed: title=%r", title, exc_info=True
                )
                return default_on_timeout

        if self._init_failed or self._impl is None or self._loop is None:
            log.warning(
                "[notify] ask_consent dropped (backend unavailable): title=%r",
                title,
            )
            return default_on_timeout
        log.info(
            "[notify] ask scheduled: title=%r yes=%r no=%r timeout=%ss",
            title,
            yes_label,
            no_label,
            timeout_secs,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._ask(title, body, yes_label, no_label, timeout_secs),
                self._loop,
            )
            result = fut.result(timeout=timeout_secs + 5.0)
            log.info("[notify] ask resolved: title=%r → %s", title, result)
            return result
        except Exception:
            log.warning("[notify] ask_consent failed", exc_info=True)
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
        """Fire-and-forget actionable toast with a single button.

        Schedules onto the existing notifier loop. Both ``on_pressed``
        (button click) and ``on_expire`` (no click within
        ``expire_after_secs``) are guarded by a single-fire latch — one
        will run, the other will not. Both callbacks execute on the
        notifier's daemon-thread asyncio loop.

        Returns ``True`` if the toast was dispatched, ``False`` if the
        backend is unavailable (init failure, dummy backend). When False
        is returned, neither callback fires — the caller must handle the
        skip itself.
        """
        if self._init_failed or self._impl is None or self._loop is None:
            log.warning(
                "[notify] notify_actionable dropped (backend unavailable): title=%r",
                title,
            )
            return False
        log.info(
            "[notify] actionable scheduled: title=%r button=%r expire_after=%ss",
            title,
            button_label,
            expire_after_secs,
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_actionable(
                    title, body, button_label, on_pressed, expire_after_secs, on_expire,
                ),
                self._loop,
            )
            return True
        except Exception:
            log.warning("[notify] actionable schedule failed", exc_info=True)
            return False

    def has_authorisation_sync(self) -> Optional[bool]:
        """Sync surface over the async ``has_authorisation`` probe.

        ``None`` means the backend can't tell us (dummy fallback, not
        booted yet). The daily-drill scheduler treats ``False`` as
        "OS-level notifications disabled" → one-time tray prompt.
        """
        if self._init_failed or self._impl is None or self._loop is None:
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._impl_has_authorisation(), self._loop
            )
            return fut.result(timeout=2.0)
        except Exception:
            log.debug("[notify] has_authorisation_sync failed", exc_info=True)
            return None

    # ---- diagnostics -------------------------------------------------------

    def diagnose(self) -> dict[str, Any]:
        """Run a full toast diagnostic and return a structured report.

        Synchronous wrapper over the async backend — safe to call from
        any thread. Sends one fire-and-forget test toast and one
        consent toast (with auto-accept on timeout) so the user can
        confirm visually whether anything appears, while every step
        also lands in ``agent.log`` for offline triage.

        The returned dict is shaped for both the
        ``sayzo-agent diagnose-notifications`` CLI and any future GUI
        surface (Settings → Permissions → "Test notifications" panel).
        """
        report: dict[str, Any] = {
            "platform": sys.platform,
            "frozen": getattr(sys, "frozen", False),
            "app_name": self._app_name,
            "init_failed": self._init_failed,
            "bundle": dict(self._bundle_info),
            "has_authorisation": None,
            "test_send": None,
            "consent_send": None,
            "errors": [],
        }
        if self._init_failed or self._impl is None or self._loop is None:
            report["errors"].append("backend unavailable (init failed or loop down)")
            return report

        # has_authorisation
        try:
            authed = asyncio.run_coroutine_threadsafe(
                self._impl_has_authorisation(), self._loop
            ).result(timeout=_SEND_TIMEOUT_SECS)
            report["has_authorisation"] = authed
            log.info("[notify] diagnose: has_authorisation=%s", authed)
        except Exception as exc:
            report["errors"].append(f"has_authorisation: {exc!r}")
            log.warning("[notify] diagnose: has_authorisation failed", exc_info=True)

        # Fire-and-forget test toast.
        try:
            send_id = asyncio.run_coroutine_threadsafe(
                self._send_with_id(
                    "Sayzo notification test",
                    "If you can read this, notifications are working.",
                ),
                self._loop,
            ).result(timeout=_SEND_TIMEOUT_SECS + 2)
            report["test_send"] = {"ok": True, "id": send_id}
            log.info("[notify] diagnose: test_send dispatched id=%s", send_id)
        except Exception as exc:
            report["test_send"] = {"ok": False, "error": repr(exc)}
            log.warning("[notify] diagnose: test_send failed", exc_info=True)

        # Consent toast — short timeout, default to "timeout" so the
        # diagnostic isn't gated on user click. We're checking that the
        # buttons render, not the click round-trip.
        try:
            consent_result = asyncio.run_coroutine_threadsafe(
                self._ask(
                    "Sayzo consent test",
                    "If you see Yes / No buttons, consent toasts work.",
                    "Yes",
                    "No",
                    3.0,
                ),
                self._loop,
            ).result(timeout=_SEND_TIMEOUT_SECS + 4)
            report["consent_send"] = {"ok": True, "result": consent_result}
            log.info(
                "[notify] diagnose: consent_send result=%s", consent_result
            )
        except Exception as exc:
            report["consent_send"] = {"ok": False, "error": repr(exc)}
            log.warning(
                "[notify] diagnose: consent_send failed", exc_info=True
            )

        return report

    # ---- loop-local coroutines ---------------------------------------------

    async def _send(self, title: str, body: str) -> None:
        """Fire-and-forget wrapper that logs but discards the identifier."""
        await self._send_with_id(title, body)

    async def _send_with_id(self, title: str, body: str) -> Optional[str]:
        """Send a toast and return the identifier on success.

        Logs every step. Wraps the underlying ``send`` call in a
        timeout so a hung completion handler shows up in the log
        within ``_SEND_TIMEOUT_SECS`` instead of leaking forever.
        """
        log.info("[notify] send begin: title=%r", title)
        try:
            assert self._impl is not None
            identifier = await asyncio.wait_for(
                self._impl.send(title=title, message=body),
                timeout=_SEND_TIMEOUT_SECS,
            )
            log.info("[notify] send done: title=%r id=%s", title, identifier)
            return identifier
        except asyncio.TimeoutError:
            log.warning(
                "[notify] send timed out after %ss: title=%r — completion "
                "handler did not fire (TCC silent-deny? main-thread run "
                "loop not pumping? bundle id mismatch?)",
                _SEND_TIMEOUT_SECS,
                title,
            )
            return None
        except Exception:
            log.warning("[notify] send failed: title=%r", title, exc_info=True)
            return None

    async def _ask(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
    ) -> ConsentResult:
        from desktop_notifier import Button

        loop = asyncio.get_running_loop()
        result_fut: asyncio.Future[ConsentResult] = loop.create_future()

        # desktop-notifier's macOS backend invokes ``on_pressed`` from
        # UNN's private dispatch queue (it doesn't marshal to our loop
        # the way the WinRT backend does). ``asyncio.Future.set_result``
        # is not thread-safe, so we hop via ``call_soon_threadsafe``;
        # on Windows the hop is redundant but harmless.
        def _resolve(value: ConsentResult) -> None:
            log.info("[notify] ask button: title=%r → %s", title, value)
            try:
                loop.call_soon_threadsafe(_set_future_safely, result_fut, value)
            except RuntimeError:
                # Loop closed mid-shutdown.
                pass

        yes_btn = Button(title=yes_label, on_pressed=lambda: _resolve("yes"))
        no_btn = Button(title=no_label, on_pressed=lambda: _resolve("no"))

        log.info("[notify] ask send begin: title=%r", title)
        try:
            assert self._impl is not None
            identifier = await asyncio.wait_for(
                self._impl.send(
                    title=title, message=body, buttons=[yes_btn, no_btn]
                ),
                timeout=_SEND_TIMEOUT_SECS,
            )
            log.info(
                "[notify] ask send done: title=%r id=%s", title, identifier
            )
        except asyncio.TimeoutError:
            log.warning(
                "[notify] ask send timed out after %ss: title=%r",
                _SEND_TIMEOUT_SECS,
                title,
            )
            return "timeout"
        except Exception:
            log.warning("[notify] ask send failed: title=%r", title, exc_info=True)
            return "timeout"

        try:
            return await asyncio.wait_for(result_fut, timeout=timeout_secs)
        except asyncio.TimeoutError:
            log.info("[notify] ask timed out (no click): title=%r", title)
            return "timeout"

    async def _send_actionable(
        self,
        title: str,
        body: str,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]],
    ) -> None:
        """Loop-local: dispatch an actionable toast + watchdog."""
        from desktop_notifier import Button

        loop = asyncio.get_running_loop()
        latch = {"fired": False}
        # Hold a reference to the watchdog handle so the on_pressed callback
        # can cancel it on click.
        timer_handle: dict[str, Any] = {"h": None}

        def _fire_pressed() -> None:
            if latch["fired"]:
                return
            latch["fired"] = True
            log.info("[notify] actionable pressed: title=%r", title)
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
                    "[notify] actionable on_pressed raised: title=%r",
                    title,
                    exc_info=True,
                )

        def _fire_expire() -> None:
            if latch["fired"]:
                return
            latch["fired"] = True
            log.info("[notify] actionable expired: title=%r", title)
            if on_expire is None:
                return
            try:
                on_expire()
            except Exception:
                log.warning(
                    "[notify] actionable on_expire raised: title=%r",
                    title,
                    exc_info=True,
                )

        # desktop-notifier dispatches on_pressed off-loop on macOS — same
        # pattern as ask_consent. Hop back via call_soon_threadsafe so the
        # latch + timer-cancel run on the loop thread.
        def _on_button() -> None:
            try:
                loop.call_soon_threadsafe(_fire_pressed)
            except RuntimeError:
                pass

        button = Button(title=button_label, on_pressed=_on_button)

        log.info("[notify] actionable send begin: title=%r", title)
        try:
            assert self._impl is not None
            identifier = await asyncio.wait_for(
                self._impl.send(title=title, message=body, buttons=[button]),
                timeout=_SEND_TIMEOUT_SECS,
            )
            log.info(
                "[notify] actionable send done: title=%r id=%s", title, identifier
            )
        except asyncio.TimeoutError:
            log.warning(
                "[notify] actionable send timed out after %ss: title=%r",
                _SEND_TIMEOUT_SECS,
                title,
            )
            _fire_expire()
            return
        except Exception:
            log.warning(
                "[notify] actionable send failed: title=%r", title, exc_info=True
            )
            _fire_expire()
            return

        timer_handle["h"] = loop.call_later(expire_after_secs, _fire_expire)


def _set_future_safely(
    fut: "asyncio.Future[ConsentResult]", value: ConsentResult
) -> None:
    """call_soon_threadsafe target — guard against double-resolve."""
    if not fut.done():
        fut.set_result(value)


def make_notifier(app_name: str = APP_AUMID) -> Notifier:
    """Construct the right notifier for the current platform + bundle state.

    On macOS, ``UNUserNotificationCenter`` (the only backend
    ``desktop-notifier`` ships) silently drops every notification when
    the host bundle is unsigned. We can't fix that from Python — it's
    the OS notification daemon refusing to render.

    Production releases are Developer-ID-signed + Apple-notarized in CI
    (see ``.github/workflows/build.yml``), so ``is_signed_bundle()``
    returns True and we go through ``DesktopNotifier`` /
    ``UNUserNotificationCenter`` — the modern, supported path.

    For unsigned dev builds (running from source, or a local PyInstaller
    build that skipped CI signing), we detect the unsigned-bundle case
    and route through :class:`MacUnsignedNotifier`, which uses the older
    ``NSUserNotification`` API (deprecated since macOS 11 but still
    functional and signing-lax through macOS 15). This keeps dev
    workflows visible without requiring an Apple Developer account
    locally.
    """
    if sys.platform == "darwin":
        try:
            from desktop_notifier.backends.macos_support import (  # type: ignore[import-not-found]
                is_bundle,
                is_signed_bundle,
            )

            in_bundle = bool(is_bundle())
            signed = bool(is_signed_bundle()) if in_bundle else False
            log.info(
                "[notify] make_notifier: is_bundle=%s is_signed=%s",
                in_bundle,
                signed,
            )
            if in_bundle and not signed:
                log.warning(
                    "[notify] Bundle is UNSIGNED — UNUserNotificationCenter "
                    "would silently drop every toast. Falling back to "
                    "NSUserNotification (deprecated but functional). "
                    "This path is for dev builds only; production releases "
                    "are signed + notarized in CI."
                )
                from .notify_mac_unsigned import MacUnsignedNotifier

                return MacUnsignedNotifier(app_name=app_name)
        except Exception:
            log.warning(
                "[notify] make_notifier: signing check failed; using default backend",
                exc_info=True,
            )
    return DesktopNotifier(app_name=app_name)
