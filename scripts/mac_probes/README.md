# Mac meeting-detection probes

Five standalone scripts to figure out *why* macOS meeting detection
isn't working in `sayzo-agent`, and decide what should replace it.

Each script answers ONE question. Run them in order; report the output
back. Don't need the full `sayzo-agent` repo on the Mac — just this
folder.

## Setup on the remote Mac (one-time, ~2 minutes)

```bash
# 1. Make sure you're on macOS 14.4+ (required for the per-process audio API).
sw_vers -productVersion

# 2. Make a fresh venv anywhere — these scripts don't touch the agent install.
python3 -m venv ~/sayzo-probes-venv
source ~/sayzo-probes-venv/bin/activate

# 3. Install the only deps they need.
pip install --upgrade pip
pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices psutil

# 4. Copy this whole folder onto the Mac. Two easy options:
#
#    Option A (rsync from your Windows box if you've got SSH set up):
#      rsync -av scripts/mac_probes/ user@mac:~/mac_probes/
#
#    Option B (paste each file into ~/mac_probes/ via the remote-access tool).
#
#    Either way, you should end up with:
#      ~/mac_probes/_common.py
#      ~/mac_probes/01_mic_active.py
#      ~/mac_probes/02_audio_processes.py
#      ~/mac_probes/03_foreground_running_titles.py
#      ~/mac_probes/04_match_simulation.py
#      ~/mac_probes/05_browser_url_attempts.py

# 5. cd into it.
cd ~/mac_probes
```

## Permissions you'll likely need to grant

Some probes use OS APIs that need TCC (privacy) approval:

- **Accessibility** — needed for any AX (window-title) reads in scripts
  03, 04, 05. macOS will silently return empty strings if not granted.
  System Settings → Privacy & Security → Accessibility → toggle ON for
  whichever Python interpreter you're running (usually `/Library/.../python3.x`
  or `/Applications/Utilities/Terminal.app`).

- **Automation** — script 05 Method C only. macOS will pop a dialog
  the first time per browser ("Python wants to control Google Chrome").
  Click OK if you want to test it; otherwise pass `--skip-applescript`.

- **Microphone** — NOT needed for any of these probes. They observe
  whether OTHER apps are using the mic; they never open a stream.

## Run order

Each script's docstring has full details — `head -40 NN_*.py` to read it.
Quick summary:

### 1. Foundation: `01_mic_active.py`

```bash
python3 01_mic_active.py --watch
```

Then join a Zoom / Meet / Discord call → `mic_active = True`.
Leave the call → `mic_active = False`. Mute/unmute → stays True
(muted users still capture).

**If the bit doesn't flip when you join a call**, the entire macOS
detection design needs to change foundation. Report back.

### 2. KEY EXPERIMENT: `02_audio_processes.py`

```bash
# One-shot showing only processes currently capturing input:
python3 02_audio_processes.py

# Live view:
python3 02_audio_processes.py --watch

# Show every audio process, even idle ones:
python3 02_audio_processes.py --show-all
```

**This is the script that decides the architecture.** If, when you join
a Zoom call, you see a row appear:

```
   PID   in  out  run  bundle_id / proc
  1234  YES   no  YES  us.zoom.xos  [zoom.us]
```

…then per-process mic attribution works on this Mac, and the detection
rewrite is small + obvious (just do what Windows does).

If `is_running_input` stays `no` for the meeting app, that's the
critical finding — try `--show-all` and look for what *did* light up.

Test scenarios:
- Zoom desktop call    → expect `us.zoom.xos` with input=YES
- Discord voice call   → expect `com.hnc.Discord` with input=YES
- Google Meet (Chrome) → expect `com.google.Chrome` with input=YES
- Microsoft Teams      → expect `com.microsoft.teams2` with input=YES
- FaceTime             → expect `com.apple.FaceTime` with input=YES

### 3. Bundle IDs + AX titles: `03_foreground_running_titles.py`

```bash
python3 03_foreground_running_titles.py
python3 03_foreground_running_titles.py --watch
```

Verifies:
- The bundle IDs we ship in the default `DetectorSpec` list actually
  match what's running on this Mac (mostly catches Discord which has
  flipped bundle IDs in the past).
- AX returns browser window titles (this is how Meet/Zoom-web detection
  works in the absence of URL reads).

If the section "AX window titles per browser" is empty when you have
browsers open, **Accessibility permission isn't granted** to the Python
interpreter — fix that before running script 04 / 05.

### 4. End-to-end simulation: `04_match_simulation.py`

```bash
python3 04_match_simulation.py --watch
```

Runs the agent's CURRENT match logic AND a PROPOSED rewrite (using the
per-process API from script 02) against the live system every 2 s, side
by side. Output looks like:

```
  CURRENT  → NO MATCH    [current: foreground is not a browser AND no
                          whitelisted desktop app is foreground.]
  PROPOSED → zoom        [proposed matched (desktop):
                          bundle_id=us.zoom.xos is_running_input=YES]

  >>> proposed approach would have fired a consent toast here, current does NOT.
```

Test scenarios (each one is a useful data point):
- Open Zoom, join a call, focus on Zoom → both should match.
- Same call, alt+tab to Notes → CURRENT goes to NO MATCH, PROPOSED stays.
- Open Google Meet in Chrome, focus on Chrome → both should match.
- Same meet, alt+tab to Slack → CURRENT goes to NO MATCH, PROPOSED stays.
- Discord voice call, focus on Discord, mute mic → both stay matched.
- ChatGPT voice mode in browser → either both, or PROPOSED only,
  depending on title patterns.

If you see CURRENT match but PROPOSED doesn't, that's a bug to dig into.

### 5. Optional — browser URL reads: `05_browser_url_attempts.py`

```bash
# Skip the TCC-dialog method by default:
python3 05_browser_url_attempts.py --skip-applescript

# OR, accept the dialogs to verify what a "correct" answer looks like:
python3 05_browser_url_attempts.py
```

Tells us whether we can read the active tab URL on macOS without an
Automation TCC prompt. Only matters for user-added URL detectors (the
Settings → Web tab UX), which currently don't work on Mac at all.

Open a known URL like `https://meet.google.com/abc-defg-hij` in each
browser before running so you can verify the readouts.

## What to send back

For each probe you run, copy-paste the output (or the relevant chunk).
The most important data points are:

1. From script 01: does `mic_active` flip when you join/leave a call?
2. From script 02: does `is_running_input=YES` light up for the meeting
   app's bundle id?
3. From script 04: do you see "PROPOSED matched" while CURRENT was NO
   MATCH? In which scenarios?

Once we know those answers we can write the actual fix instead of
guessing.
