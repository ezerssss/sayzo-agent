"""Unit tests for sayzo_agent.gui.setup.mac_permissions.

Runs on any platform — the OS-specific APIs (sounddevice, audio-tap,
desktop-notifier) are all mocked. The module under test also short-circuits
when ``sys.platform != 'darwin'``, so we patch that at module level for the
macOS-path tests.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sayzo_agent.gui.setup import mac_permissions


@pytest.fixture(autouse=True)
def _reset_notifier_singleton():
    """Each test gets a fresh DesktopNotifierSync singleton so mocked
    backends don't leak between cases."""
    mac_permissions._NOTIFIER = None
    mac_permissions._NOTIFIER_INIT_FAILED = False
    yield
    mac_permissions._NOTIFIER = None
    mac_permissions._NOTIFIER_INIT_FAILED = False


# ---------------------------------------------------------------------------
# prompt_microphone
# ---------------------------------------------------------------------------


def _fake_avfoundation(
    *,
    status: int,
    request_response: bool | None = None,
    request_delay_secs: float = 0.0,
) -> SimpleNamespace:
    """Build a minimal AVFoundation-shaped module the helper can import.

    ``status`` is the value AVCaptureDevice.authorizationStatusForMediaType_
    will return (0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized).
    When status==0 (NotDetermined), ``request_response`` controls whether
    the simulated dialog returns ``True`` (allow), ``False`` (deny), or
    ``None`` (handler never fires — simulates timeout). ``request_delay_secs``
    controls how long the simulated dialog takes before firing the
    completion handler — used to drive the stale-TCC heuristic, which
    treats sub-500 ms False responses as silent-denies.
    """
    captured: dict = {}

    class _AVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type):
            captured["status_query"] = media_type
            return status

        @staticmethod
        def requestAccessForMediaType_completionHandler_(
            media_type, completion
        ):
            captured["request_query"] = media_type
            captured["completion"] = completion
            if request_response is not None:
                # Fire the handler on a worker thread so the caller's
                # event.wait actually unblocks. Real macOS dispatches on
                # a private background queue per Apple's docs. Sleep
                # first so tests of the stale-TCC heuristic can simulate
                # a "fast silent deny" vs. a slower legitimate click.
                import threading as _t
                import time as _time

                def _fire():
                    if request_delay_secs:
                        _time.sleep(request_delay_secs)
                    completion(request_response)

                _t.Thread(target=_fire, daemon=True).start()

    return SimpleNamespace(
        AVCaptureDevice=_AVCaptureDevice,
        AVMediaTypeAudio="soun",
        _captured=captured,
    )


def _fake_foundation(
    *,
    fire_block: bool = True,
) -> SimpleNamespace:
    """Build a minimal Foundation-shaped module exposing NSOperationQueue.

    The helper schedules the AVFoundation request onto
    ``NSOperationQueue.mainQueue()`` so a frozen pywebview bundle can present
    the TCC dialog (the framework silently no-ops the dialog when
    requestAccessForMediaType is invoked off the main thread).

    ``fire_block=True`` runs the scheduled block immediately (simulating a
    pumping main runloop). ``fire_block=False`` drops it on the floor (the
    main runloop never pumps), which lets us cover the "dispatched but
    nothing fired" timeout branch.
    """

    class _MainQueue:
        @staticmethod
        def addOperationWithBlock_(block):
            if fire_block:
                # Run on a worker thread to mimic NSOperationQueue's async
                # semantics — addOperationWithBlock_ returns immediately on
                # the real API.
                import threading as _t
                _t.Thread(target=block, daemon=True).start()

    class _NSOperationQueue:
        @staticmethod
        def mainQueue():
            return _MainQueue

    return SimpleNamespace(NSOperationQueue=_NSOperationQueue)


def _patch_av(fake_av, *, fake_foundation_mod=None):
    """Bundle the patches a prompt_microphone test needs.

    ``fake_av`` is the AVFoundation stand-in. ``fake_foundation_mod`` is
    the Foundation stand-in (defaults to one whose mainQueue runs blocks
    synchronously on a worker thread)."""
    if fake_foundation_mod is None:
        fake_foundation_mod = _fake_foundation()
    return [
        patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"),
        patch.dict(
            "sys.modules",
            {
                "AVFoundation": fake_av,
                "Foundation": fake_foundation_mod,
            },
        ),
    ]


def test_prompt_microphone_returns_true_when_already_authorized():
    """Status == 3 (Authorized): no dialog fired, returns True directly."""
    fake_av = _fake_avfoundation(status=3)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is True
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)
    assert "request_query" not in fake_av._captured


def test_prompt_microphone_returns_false_when_previously_denied():
    """Status == 2 (Denied): no dialog can be re-fired, returns False.

    A previously-recorded denial is NOT the stale-TCC pattern — the user
    saw the dialog at some point and chose Don't Allow. stale_tcc_likely
    must stay False so the GUI shows the standard "blocked" copy, not the
    "remove from System Settings" recovery flow.
    """
    fake_av = _fake_avfoundation(status=2)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_false_when_restricted():
    """Status == 1 (Restricted, e.g. MDM/parental controls): returns False."""
    fake_av = _fake_avfoundation(status=1)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_user_decision_when_not_determined():
    """Status == 0 (NotDetermined): fire dialog, return user's actual click.

    This is the core regression test. Old code returned True after 100 ms
    sleep regardless of the user's eventual click — even when they hit
    Don't Allow — which made setup mark mic as granted on a denied bundle.
    """
    fake_av = _fake_avfoundation(status=0, request_response=True)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is True
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)
    assert fake_av._captured["request_query"] == "soun"

    # A real human deny click takes much longer than the 500 ms threshold,
    # so a False here represents a legitimate denial and stale_tcc_likely
    # must stay False. Simulate the elapsed time by having the request
    # delay before firing the completion handler.
    fake_av = _fake_avfoundation(
        status=0, request_response=False, request_delay_secs=0.6
    )
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_flags_stale_tcc_on_fast_silent_deny():
    """The Sheen v2.7.0 case. Status read returns NotDetermined, the
    request branch fires the completion handler with False in <500 ms
    (no human can read+click that fast). That's the fingerprint of a
    stale TCC entry from a prior Sayzo install with a different signing
    identity silently denying the request without UI. The GUI uses this
    flag to swap the misleading "open Settings, turn it on" copy for the
    targeted "remove from System Settings, then retry" recovery flow.
    """
    fake_av = _fake_avfoundation(
        status=0, request_response=False, request_delay_secs=0.0
    )
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is True
    finally:
        _exit_all(patches)


def test_prompt_microphone_does_not_flag_stale_when_user_grants_quickly():
    """Sub-500 ms approval is implausible for a human but possible in
    automated UI tests. Either way it's NOT stale-TCC since the result is
    True. Defensive: stale_tcc_likely must only fire on False results."""
    fake_av = _fake_avfoundation(
        status=0, request_response=True, request_delay_secs=0.0
    )
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is True
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_dispatches_request_to_main_queue():
    """Regression for v2.7.0 Sheen bug.

    requestAccessForMediaType_completionHandler_ MUST be invoked from a
    block scheduled on NSOperationQueue.mainQueue(), not from pywebview's
    JS-RPC worker thread. From the worker thread the AVFoundation
    framework silently no-ops the dialog and fires the completion handler
    with `denied` in milliseconds. We verify here that the helper goes
    through addOperationWithBlock_ for the request — without it, the
    fix has regressed.
    """
    main_queue_calls: list = []

    class _CapturingMainQueue:
        @staticmethod
        def addOperationWithBlock_(block):
            main_queue_calls.append(block)
            # Run the block on a worker thread so the helper unblocks.
            import threading as _t
            _t.Thread(target=block, daemon=True).start()

    capturing = SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: _CapturingMainQueue)
    )
    fake_av = _fake_avfoundation(status=0, request_response=True)
    patches = _patch_av(fake_av, fake_foundation_mod=capturing)
    _enter_all(patches)
    try:
        assert mac_permissions.prompt_microphone().granted is True
    finally:
        _exit_all(patches)
    # One block scheduled on the main queue: the request itself.
    assert len(main_queue_calls) == 1
    assert callable(main_queue_calls[0])


def test_prompt_microphone_returns_none_on_dialog_timeout():
    """Status == 0 but the completion handler never fires — likely the
    dialog never actually presented (signing or bundle config issue).
    Return None so the GUI can route to Open-Settings."""
    # request_response=None → handler is never called.
    fake_av = _fake_avfoundation(status=0, request_response=None)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        # Patch the timeout down so the test runs in milliseconds.
        with patch(
            "sayzo_agent.gui.setup.mac_permissions._TCC_REQUEST_TIMEOUT_SECS",
            0.05,
        ):
            result = mac_permissions.prompt_microphone()
            assert result.granted is None
            assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_none_when_main_queue_drops_block():
    """Defensive: if NSOperationQueue.mainQueue silently drops the block
    (e.g. main runloop isn't pumping for some reason), the completion
    handler never fires and we should hit the timeout, returning None."""
    # fire_block=False → addOperationWithBlock_ is a no-op.
    fake_foundation = _fake_foundation(fire_block=False)
    fake_av = _fake_avfoundation(status=0, request_response=True)
    patches = _patch_av(fake_av, fake_foundation_mod=fake_foundation)
    _enter_all(patches)
    try:
        with patch(
            "sayzo_agent.gui.setup.mac_permissions._TCC_REQUEST_TIMEOUT_SECS",
            0.05,
        ):
            assert mac_permissions.prompt_microphone().granted is None
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_none_on_unexpected_status():
    """Defensive: an enum value Apple hasn't documented yet (e.g. 99)
    must not be silently bucketed as granted/denied."""
    fake_av = _fake_avfoundation(status=99)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        assert mac_permissions.prompt_microphone().granted is None
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_none_when_avfoundation_unavailable():
    """Dev machine without pyobjc-framework-AVFoundation: log warn,
    return None — never raise back into the bridge."""
    # Force the import to fail by injecting a module that re-raises.
    failing_module = SimpleNamespace()
    # AttributeError on access to AVCaptureDevice — the helper should
    # catch the broad Exception and return None.
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"AVFoundation": failing_module}):
        result = mac_permissions.prompt_microphone()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_microphone_returns_none_when_foundation_unavailable():
    """Dev machine where pyobjc-framework-Cocoa (Foundation) didn't load:
    we can't dispatch onto the main queue, so we bail with None rather
    than fire the request from the worker thread (where the dialog
    silently no-ops)."""
    fake_av = _fake_avfoundation(status=0, request_response=True)
    failing_foundation = SimpleNamespace()  # no NSOperationQueue attribute
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict(
        "sys.modules",
        {"AVFoundation": fake_av, "Foundation": failing_foundation},
    ):
        assert mac_permissions.prompt_microphone().granted is None


def test_prompt_microphone_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        result = mac_permissions.prompt_microphone()
        assert result.granted is None
        assert result.stale_tcc_likely is False


# ---------------------------------------------------------------------------
# prompt_audio_capture
# ---------------------------------------------------------------------------


class _FakeAudioTapProc:
    """Stand-in for subprocess.Popen([audio-tap]). Drives the helper through
    a scripted stderr stream + exit code so tests cover all decision branches
    without spawning a real binary.
    """

    def __init__(
        self,
        *,
        stderr_lines: list[str],
        exit_code: int | None,
        line_delay_secs: float = 0.0,
    ) -> None:
        self.pid = 4242
        self._stderr_lines = list(stderr_lines)
        self._exit_code = exit_code
        self._line_delay = line_delay_secs
        self.stderr = self  # iterable for `for line in proc.stderr:`
        self.stdout = None
        self._terminated = False
        self._exited_event = __import__("threading").Event()
        # Mark exited synchronously when an exit_code is provided; the helper
        # may call wait() before consuming stderr.
        if exit_code is not None and not stderr_lines:
            self._exited_event.set()

    def __iter__(self):
        return self

    def __next__(self):
        if not self._stderr_lines:
            # EOF on stderr — also flips us to "exited" if we have a code.
            if self._exit_code is not None:
                self._exited_event.set()
            raise StopIteration
        if self._line_delay:
            import time as _t
            _t.sleep(self._line_delay)
        return self._stderr_lines.pop(0)

    def poll(self):
        return self._exit_code if self._exited_event.is_set() else None

    def wait(self, timeout: float | None = None):
        if self._exited_event.wait(timeout=timeout):
            return self._exit_code if self._exit_code is not None else 0
        raise subprocess.TimeoutExpired(cmd="audio-tap", timeout=timeout)

    def terminate(self):
        self._terminated = True
        # Simulate the binary exiting promptly on SIGTERM.
        if self._exit_code is None:
            self._exit_code = 0
        self._exited_event.set()

    def kill(self):
        self.terminate()


def _patch_for_audio_tap(fake_proc: _FakeAudioTapProc | OSError | None):
    """Context-manager helper bundling all the patches the audio-tap probe
    needs. Pass a fake proc to simulate spawn success, an OSError to simulate
    spawn failure, or None to leave subprocess.Popen unpatched (used by the
    binary-missing path)."""
    patches = [
        patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"),
        patch(
            "sayzo_agent.capture.system_mac._find_audio_tap",
            return_value="/fake/audio-tap",
        ),
    ]
    if isinstance(fake_proc, OSError):
        patches.append(
            patch(
                "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen",
                side_effect=fake_proc,
            )
        )
    elif fake_proc is not None:
        patches.append(
            patch(
                "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen",
                return_value=fake_proc,
            )
        )
    return patches


def _enter_all(patches):
    """tiny helper: enter a list of patch context managers in one block."""
    return [p.start() for p in patches]


def _exit_all(patches):
    for p in patches:
        p.stop()


def test_prompt_audio_capture_returns_true_on_success_line():
    """Granted path: stderr emits 'capturing system audio …', the helper
    sees it, kills the still-running probe, and returns True."""
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: using global tap\n",
            "audio-tap: capturing system audio (native 48000 Hz ...)\n",
        ],
        exit_code=None,  # Real binary keeps running until we SIGTERM it.
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is True
        assert result.stale_tcc_likely is False
        assert fake._terminated  # Helper SIGTERM'd the still-running binary.
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_false_on_exit_77():
    """Denied path with a real human click delay: stderr emits the
    'AudioHardwareCreateProcessTap failed' line and the binary exits with
    77 after the user clicks Don't Allow. The helper returns False with
    stale_tcc_likely=False since the elapsed time exceeds the threshold —
    a real click can't happen sub-500 ms.
    """
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: AudioHardwareCreateProcessTap failed (OSStatus -1719).\n",
        ],
        exit_code=77,
        # Hold the dialog open past the 500 ms stale-TCC threshold.
        line_delay_secs=0.6,
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_flags_stale_tcc_on_fast_silent_deny():
    """Stale-TCC fingerprint: pre-v2.6.0 ad-hoc-signed audio-tap left
    a TCC entry whose code-requirement no longer matches the current
    Developer-ID-signed binary. macOS silently denies without ever
    presenting a dialog, so the binary exits 77 in milliseconds. The
    helper flags stale_tcc_likely so the GUI can show the targeted
    "remove from System Settings → Audio Capture, then retry" copy
    instead of the misleading "open Settings, turn it on" message.
    """
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: AudioHardwareCreateProcessTap failed (OSStatus -1719).\n",
        ],
        exit_code=77,
        line_delay_secs=0.0,  # Instant deny — no dialog.
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is False
        assert result.stale_tcc_likely is True
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_when_dialog_times_out():
    """Inconclusive: no decisive stderr, binary still alive when the
    bridge timeout fires. Helper terminates the probe and returns None."""
    fake = _FakeAudioTapProc(
        stderr_lines=[],  # Empty stderr → reader blocks until EOF/SIGTERM.
        exit_code=None,
        # Slow drip so reader blocks even if we ever add lines.
        line_delay_secs=10.0,
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        with patch(
            "sayzo_agent.gui.setup.mac_permissions._TCC_REQUEST_TIMEOUT_SECS",
            0.05,
        ):
            result = mac_permissions.prompt_audio_capture()
            assert result.granted is None
            assert result.stale_tcc_likely is False
        assert fake._terminated
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_on_unexpected_exit():
    """Binary exits non-zero non-77 (e.g. SIGABRT from Gatekeeper on a
    managed Mac) before any decisive stderr. Treat as inconclusive."""
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: dyld error\n",
        ],
        exit_code=-6,  # SIGABRT
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_when_binary_missing():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap",
        side_effect=FileNotFoundError("no binary"),
    ):
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_audio_capture_returns_none_on_spawn_failure():
    patches = _patch_for_audio_tap(OSError("permission denied"))
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False


# ---------------------------------------------------------------------------
# prompt_notifications
# ---------------------------------------------------------------------------


def _patch_notifier(authorise_return: bool | Exception):
    """Return a context manager that patches DesktopNotifierSync in sys.modules
    with a backend whose request_authorisation returns ``authorise_return`` (or
    raises it, if it's an Exception)."""
    fake = MagicMock()
    if isinstance(authorise_return, Exception):
        fake.request_authorisation.side_effect = authorise_return
    else:
        fake.request_authorisation.return_value = authorise_return
    module = SimpleNamespace(DesktopNotifierSync=MagicMock(return_value=fake))
    return patch.dict("sys.modules", {"desktop_notifier.sync": module}), fake


def test_prompt_notifications_returns_true_when_granted():
    sys_modules_patch, _ = _patch_notifier(True)
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        result = mac_permissions.prompt_notifications()
        assert result.granted is True
        assert result.stale_tcc_likely is False


def test_prompt_notifications_returns_false_when_denied_slowly():
    """Real human-click denial: stale_tcc_likely stays False because the
    elapsed time exceeds the 500 ms threshold."""
    fake = MagicMock()

    def _slow_deny():
        import time as _t
        _t.sleep(0.6)
        return False

    fake.request_authorisation.side_effect = _slow_deny
    module = SimpleNamespace(DesktopNotifierSync=MagicMock(return_value=fake))
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"desktop_notifier.sync": module}):
        result = mac_permissions.prompt_notifications()
        assert result.granted is False
        assert result.stale_tcc_likely is False


def test_prompt_notifications_flags_stale_tcc_on_fast_silent_deny():
    """Stale UNN entry from a previous Sayzo install with a different
    signing identity silently denies without UI. request_authorisation
    returns False instantly. The helper flags stale_tcc_likely so the
    bridge payload exposes it (the React Notifications screen still falls
    back to the existing waiting-state polling, since the System Settings
    Notifications toggle DOES re-record under the new CR — but the flag
    is plumbed through for diagnostics + future refinement)."""
    sys_modules_patch, _ = _patch_notifier(False)
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        result = mac_permissions.prompt_notifications()
        assert result.granted is False
        assert result.stale_tcc_likely is True


def test_prompt_notifications_returns_none_on_backend_error():
    sys_modules_patch, _ = _patch_notifier(RuntimeError("boom"))
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        result = mac_permissions.prompt_notifications()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_notifications_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        result = mac_permissions.prompt_notifications()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_notifications_returns_none_when_init_fails():
    """If DesktopNotifierSync construction throws, the helper must swallow
    and return None (never raise back into the bridge)."""
    failing_ctor = MagicMock(side_effect=RuntimeError("no backend"))
    module = SimpleNamespace(DesktopNotifierSync=failing_ctor)
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"desktop_notifier.sync": module}):
        result = mac_permissions.prompt_notifications()
        assert result.granted is None
        assert result.stale_tcc_likely is False


# ---------------------------------------------------------------------------
# open_* helpers
# ---------------------------------------------------------------------------


def test_open_mic_settings_spawns_open_on_darwin():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_mic_settings()
    assert popen.call_count == 1
    args = popen.call_args.args[0]
    assert args[0] == "open"
    assert "Privacy_Microphone" in args[1]


def test_open_audio_capture_settings_uses_audio_capture_uri():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_audio_capture_settings()
    args = popen.call_args.args[0]
    assert "Privacy_AudioCapture" in args[1]


def test_open_notification_settings_uses_notifications_uri():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_notification_settings()
    args = popen.call_args.args[0]
    assert "Notifications-Settings" in args[1]


def test_open_helpers_are_noop_on_non_darwin():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_mic_settings()
        mac_permissions.open_audio_capture_settings()
        mac_permissions.open_notification_settings()
    assert popen.called is False


def test_open_swallows_oserror():
    """subprocess.Popen can raise OSError if ``open`` is unavailable — we
    must not propagate, since the helpers are invoked from the JS bridge."""
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen",
        side_effect=OSError("no open binary"),
    ):
        # Should not raise.
        mac_permissions.open_mic_settings()
