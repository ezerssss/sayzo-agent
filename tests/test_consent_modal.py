"""Tests for the macOS consent-dialog osascript output parser.

The parser used to ``re.search(r"button returned:([^,\\n]+)", output)``,
which split on the first comma — so a label that itself contained a
comma (e.g. ``"Yes, stop"``, ``"Yes, start"``, ``"Yes, done"``,
``"Yes, keep going"``) was captured as just ``"Yes"``, didn't match
``yes_label``, and silently fell through to ``default_on_timeout``. For
the disarm-confirm toast that meant the user clicking "Yes, stop" was
parsed as "no" and recording kept running.

These tests stub ``subprocess.run`` so they exercise the parser on
every platform, not just macOS — only the early ``sys.platform`` guard
is mac-specific, and we monkeypatch that too.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional

import pytest

import sayzo_agent.consent_modal as consent_modal
from sayzo_agent.consent_modal import consent_modal_macos


@dataclass
class _FakeCompleted:
    returncode: int
    stdout: str
    stderr: str = ""


def _patch_platform_darwin(monkeypatch) -> None:
    """The early-out at the top of ``consent_modal_macos`` returns
    ``default_on_timeout`` on non-darwin platforms; tests need to bypass it
    so the parser actually runs in CI / on Windows."""
    monkeypatch.setattr(consent_modal.sys, "platform", "darwin")


def _patch_subprocess(monkeypatch, completed: _FakeCompleted) -> list[list[str]]:
    """Replace ``subprocess.run`` with a fake that returns ``completed``
    and records the argv list it was called with. Returns the list so
    individual tests can assert on the script that was sent to osascript."""
    captured: list[list[str]] = []

    def _fake_run(argv, **_kwargs):
        captured.append(list(argv))
        return completed

    monkeypatch.setattr(consent_modal.subprocess, "run", _fake_run)
    return captured


# ---------------------------------------------------------------------------
# The actual bug: comma in yes_label → user clicked Yes, but parser said no.
# ---------------------------------------------------------------------------


def test_yes_label_with_comma_parses_as_yes(monkeypatch):
    """Regression: 'Yes, stop' on the disarm-confirm toast must parse as
    'yes'. Previously the regex captured 'Yes', mismatched the label, and
    the dialog fell through to default_on_timeout='no' — so clicking
    'Yes, stop' silently kept recording running."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(
            returncode=0,
            stdout="button returned:Yes, stop, gave up:false\n",
        ),
    )

    result = consent_modal_macos(
        title="Stop recording?",
        body="We'll save what we've captured so far.",
        yes_label="Yes, stop",
        no_label="Keep going",
        timeout_secs=30.0,
        default_on_timeout="no",
    )
    assert result == "yes"


@pytest.mark.parametrize(
    "yes_label,no_label,stdout,expected",
    [
        # Every comma-bearing label that ships in the real ArmController.
        ("Yes, stop", "Keep going",
         "button returned:Yes, stop, gave up:false", "yes"),
        ("Yes, start", "Cancel",
         "button returned:Yes, start, gave up:false", "yes"),
        ("Yes, done", "Not yet",
         "button returned:Yes, done, gave up:false", "yes"),
        ("Yes, keep going", "Wrap up",
         "button returned:Yes, keep going, gave up:false", "yes"),
    ],
)
def test_all_real_comma_labels_parse_correctly(
    monkeypatch, yes_label, no_label, stdout, expected,
):
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(monkeypatch, _FakeCompleted(returncode=0, stdout=stdout))
    assert (
        consent_modal_macos(
            "Title", "Body", yes_label, no_label,
            timeout_secs=10.0, default_on_timeout="no",
        )
        == expected
    )


# ---------------------------------------------------------------------------
# Existing happy paths shouldn't regress.
# ---------------------------------------------------------------------------


def test_simple_yes_label_parses_as_yes(monkeypatch):
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(
            returncode=0,
            stdout="button returned:Start coaching, gave up:false",
        ),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Start coaching", "Not now", 30.0, "no",
        )
        == "yes"
    )


def test_no_label_parses_as_no_when_returned_in_stdout(monkeypatch):
    """If the user clicks no_label without the cancel-button mapping
    kicking in (e.g. a future caller chooses not to set ``cancel button``),
    the parser must still recognise the label."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(
            returncode=0,
            stdout="button returned:Wrap up, gave up:false",
        ),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Keep going", "Wrap up", 30.0, "yes",
        )
        == "no"
    )


def test_cancel_button_returns_no(monkeypatch):
    """Clicking the cancel button (or pressing Esc / Cmd-.) makes
    osascript exit with rc=1 and an empty stdout — which we map to 'no'."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=1, stdout="", stderr="execution error: User canceled."),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 30.0, "no",
        )
        == "no"
    )


def test_giving_up_returns_timeout(monkeypatch):
    """``giving up after`` firing produces ``button returned:, gave up:true``
    (empty button label) on rc=0. Must map to 'timeout' regardless of
    what the labels are."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(
            returncode=0,
            stdout="button returned:, gave up:true",
        ),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 5.0, "no",
        )
        == "timeout"
    )


def test_format_without_gave_up_marker_still_parses(monkeypatch):
    """Older AppleScript builds (and any caller without 'giving up after')
    omit the trailing ``, gave up:`` field. Make sure we still match."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=0, stdout="button returned:Yes, stop"),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 30.0, "no",
        )
        == "yes"
    )


# ---------------------------------------------------------------------------
# Failure / fallback paths.
# ---------------------------------------------------------------------------


def test_unrecognised_label_returns_default(monkeypatch):
    """If osascript echoes a label we never asked for (shouldn't happen,
    but defend against it), fall back to default_on_timeout instead of
    crashing the consent flow."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(
            returncode=0,
            stdout="button returned:Mystery, gave up:false",
        ),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 30.0, "yes",
        )
        == "yes"  # default_on_timeout
    )


def test_unparseable_output_returns_default(monkeypatch):
    """Output without the 'button returned:' prefix → can't tell what
    happened. Defaulting beats guessing."""
    _patch_platform_darwin(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=0, stdout="something completely different"),
    )
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 30.0, "no",
        )
        == "no"
    )


def test_subprocess_timeout_returns_default(monkeypatch):
    _patch_platform_darwin(monkeypatch)

    def _raises(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=1.0)

    monkeypatch.setattr(consent_modal.subprocess, "run", _raises)
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 1.0, "no",
        )
        == "no"
    )


def test_osascript_not_installed_returns_default(monkeypatch):
    _patch_platform_darwin(monkeypatch)

    def _raises(*_a, **_kw):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(consent_modal.subprocess, "run", _raises)
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 1.0, "no",
        )
        == "no"
    )


def test_non_darwin_returns_default_immediately(monkeypatch):
    """Cross-platform safety: callers should see the configured default
    (and no subprocess invocation) when this is called off-platform."""
    monkeypatch.setattr(consent_modal.sys, "platform", "win32")
    called: list[bool] = []

    def _should_not_run(*_a, **_kw):
        called.append(True)
        raise AssertionError("subprocess.run must not be called on non-darwin")

    monkeypatch.setattr(consent_modal.subprocess, "run", _should_not_run)
    assert (
        consent_modal_macos(
            "T", "B", "Yes, stop", "Keep going", 30.0, "yes",
        )
        == "yes"
    )
    assert called == []


# ---------------------------------------------------------------------------
# AppleScript-side correctness — the script we send must escape labels
# safely even when they contain characters AppleScript treats specially.
# ---------------------------------------------------------------------------


def test_script_escapes_label_quotes_and_backslashes(monkeypatch):
    _patch_platform_darwin(monkeypatch)
    captured = _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=0, stdout='button returned:Yes, gave up:false'),
    )
    consent_modal_macos(
        title='Sayzo "asks"',
        body="line 1\nline 2",
        yes_label="Yes",
        no_label="No",
        timeout_secs=10.0,
        default_on_timeout="no",
    )
    assert captured, "subprocess.run should have been invoked once"
    argv = captured[0]
    assert argv[0] == "osascript"
    script = argv[2]
    # Quotes inside the title were escaped, not raw.
    assert '\\"asks\\"' in script
    # Newlines became the AppleScript escape sequence.
    assert "line 1\\nline 2" in script
    # The literal label round-trips into the buttons + default-button clauses.
    assert '"Yes"' in script
    assert '"No"' in script


# ---------------------------------------------------------------------------
# Icon: bundled .icns when present, fallback to built-in note otherwise.
# ---------------------------------------------------------------------------


def test_script_uses_bundled_icns_when_resolver_returns_path(monkeypatch):
    """When ``_sayzo_icns_path`` finds the bundled logo, the AppleScript
    must point at it via ``POSIX file`` instead of the generic ``note``
    glyph — that's the only way to get the Sayzo logo into the dialog."""
    _patch_platform_darwin(monkeypatch)
    monkeypatch.setattr(
        consent_modal,
        "_sayzo_icns_path",
        lambda: "/Applications/Sayzo.app/Contents/Resources/logo.icns",
    )
    captured = _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=0, stdout="button returned:Yes, gave up:false"),
    )
    consent_modal_macos("T", "B", "Yes", "No", 5.0, "no")
    script = captured[0][2]
    assert (
        'with icon (POSIX file "/Applications/Sayzo.app/Contents/Resources/logo.icns")'
        in script
    )
    # And the fallback clause is NOT also present.
    assert "with icon note" not in script


def test_script_falls_back_to_note_when_no_icns_on_disk(monkeypatch):
    """Dev runs (no .icns generated yet) should still produce a valid
    dialog — we just lose the logo and use AppleScript's built-in note."""
    _patch_platform_darwin(monkeypatch)
    monkeypatch.setattr(consent_modal, "_sayzo_icns_path", lambda: None)
    captured = _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=0, stdout="button returned:Yes, gave up:false"),
    )
    consent_modal_macos("T", "B", "Yes", "No", 5.0, "no")
    script = captured[0][2]
    assert "with icon note" in script
    assert "POSIX file" not in script


def test_script_escapes_quotes_in_icns_path(monkeypatch):
    """An app installed under a path containing a quote (vanishingly rare
    but possible — e.g. ``~/Applications/"work"/Sayzo.app``) must not
    break the AppleScript out of its icon clause."""
    _patch_platform_darwin(monkeypatch)
    monkeypatch.setattr(
        consent_modal,
        "_sayzo_icns_path",
        lambda: '/path/with "quote"/logo.icns',
    )
    captured = _patch_subprocess(
        monkeypatch,
        _FakeCompleted(returncode=0, stdout="button returned:Yes, gave up:false"),
    )
    consent_modal_macos("T", "B", "Yes", "No", 5.0, "no")
    script = captured[0][2]
    assert '"/path/with \\"quote\\"/logo.icns"' in script


def test_sayzo_icns_path_returns_none_off_platform(monkeypatch):
    """The resolver short-circuits on non-darwin so callers on Windows
    / Linux never spend a stat on a path that can't matter to them."""
    monkeypatch.setattr(consent_modal.sys, "platform", "win32")
    assert consent_modal._sayzo_icns_path() is None


def test_sayzo_icns_path_finds_dev_tree_file(monkeypatch, tmp_path):
    """When a developer has generated ``installer/assets/logo.icns``
    locally (e.g. by running the CI's iconutil step), the dev path must
    pick it up so they can preview the modal with the real logo."""
    _patch_platform_darwin(monkeypatch)
    # Pretend the module lives under tmp_path/sayzo_agent/consent_modal.py
    fake_pkg = tmp_path / "sayzo_agent"
    fake_pkg.mkdir()
    fake_module = fake_pkg / "consent_modal.py"
    fake_module.write_text("# stub")
    assets = tmp_path / "installer" / "assets"
    assets.mkdir(parents=True)
    icns = assets / "logo.icns"
    icns.write_bytes(b"icns")
    monkeypatch.setattr(consent_modal, "__file__", str(fake_module))
    # Force the frozen-bundle branch to miss so we exercise the dev fallback.
    monkeypatch.setattr(consent_modal.sys, "frozen", False, raising=False)
    assert consent_modal._sayzo_icns_path() == str(icns)


def test_sayzo_icns_path_returns_none_when_nothing_on_disk(monkeypatch, tmp_path):
    """Fresh dev checkout: no .icns anywhere → caller falls back to
    ``with icon note`` rather than feeding AppleScript a bad path."""
    _patch_platform_darwin(monkeypatch)
    fake_pkg = tmp_path / "sayzo_agent"
    fake_pkg.mkdir()
    fake_module = fake_pkg / "consent_modal.py"
    fake_module.write_text("# stub")
    monkeypatch.setattr(consent_modal, "__file__", str(fake_module))
    monkeypatch.setattr(consent_modal.sys, "frozen", False, raising=False)
    assert consent_modal._sayzo_icns_path() is None
