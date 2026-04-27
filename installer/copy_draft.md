# User-facing copy — draft for review

All strings that end up in front of a user live here first. Review and revise *before* they get encoded into the NSIS script or the pywebview setup screens. Nothing in this file is shipped as-is; it's the staging area.

When you're happy with a block, mark it **APPROVED** in a comment next to it, and I'll propagate the approved text into the actual code paths.

---

## 1. Windows installer (NSIS) — `installer/windows/sayzo-agent.nsi`

### Welcome page

> **Sayzo — the English speaking coach you bring to your meetings.**
>
> Sayzo captures conversations from your meetings and turns them into personalized English-speaking drills. It only listens when you say so: press a keyboard shortcut, or say yes to a prompt when Sayzo notices you're in a meeting.
>
> Your microphone stays off until then.

### Finish page

> **Sayzo is ready.**
>
> We'll open a short setup window to get Sayzo ready. Nothing records until you say so.

---

## 2. First-run setup window (pywebview, both platforms) — `sayzo_agent/gui/webui/src/`

One window, one walkthrough. No second tkinter step after it closes — everything lives here. Opens automatically the first time the service starts (`__main__.py service`), blocks the main thread until the user clicks Got it on the Done screen.

### Shared chrome

- Window title: **Sayzo — Setup**
- Upper-left: Sayzo logo + "Sayzo"
- Each step shows a numeric indicator (`01` / `02` / …).

### macOS flow — 8 screens

1. Welcome (sign-in)
2. Download model
3. Microphone
4. Audio Capture
5. Accessibility (covers global hotkey AND web meeting detection)
6. Notifications
7. Shortcut
8. Done

### Windows flow — 5 screens

1. Welcome (sign-in)
2. Download model
3. Notifications
4. Shortcut
5. Done

### Screen 01 — Welcome (both)

- Title: **Welcome to Sayzo**
- Body (**APPROVED**): *"A quick two-minute setup. Nothing records until you say so."*
- Copy below the button row (**APPROVED**): *"Signing in links this machine to your account so your captures become coaching drills in the Sayzo web app."*
- Buttons: **Cancel** / **Sign in**
- Pending state button label: **Opening browser…**

### Screen 02 — Download (both)

- Title: **Setting things up**
- Body (**APPROVED**): *"Getting Sayzo ready — about 2 GB, one time only."*
- Buttons: **Cancel** / **Continue** (Continue disabled until done)
- Error fallback: **Download failed.** *[reason]* + **Retry** button

### Screen 03 — Microphone (macOS only)

- Title (**APPROVED**): **Microphone access**
- Body (**APPROVED**): *"Sayzo uses your mic only when you start a conversation — with your keyboard shortcut, or by accepting a prompt on screen."*
- Buttons: **Cancel** / **Allow**
- On Allow: triggers the macOS Microphone TCC dialog by briefly opening a `sounddevice.InputStream` (see `mac_permissions.prompt_microphone`). Advance is user-initiated — pressing Continue after the system dialog resolves.
- Granted state (**APPROVED**): *"All set! Your conversations are ready to become personalized speaking drills."*
- Denied state (**APPROVED**): *"Looks like macOS blocked the mic. Open System Settings, turn it on for Sayzo, then come back."* — button changes to **Open Settings**.
- No Skip option: mic access is required to use Sayzo.

### Screen 04 — Audio Capture (macOS only)

- Title (**APPROVED**): **System audio access**
- Body (**APPROVED**): *"Sayzo captures audio from your meetings — like Zoom, Meet, or Teams — so it can transcribe the full conversation, not just your side."*
- Buttons: **Cancel** / **Allow**
- On Allow: spawns the pre-compiled audio-tap Swift helper, which triggers the Audio Capture TCC dialog via `AudioHardwareCreateProcessTap`.
- Granted state (**APPROVED**): *"All set! Your drills will now use the whole meeting — not just your side."*
- Denied state (**APPROVED**): *"Looks like macOS blocked system audio. Open System Settings, turn it on for Sayzo, then come back."*
- No Skip option: system audio access is required for drills to capture full meeting context.

### Screen 05 — Accessibility (macOS only)

- Title (**APPROVED**): **Accessibility access**
- Body (**APPROVED**): *"Lets your keyboard shortcut start Sayzo from any app, and helps Sayzo notice when you're in a web meeting (Meet, Zoom web, Teams web). Only the shortcut you picked wakes Sayzo up — anything else you type goes nowhere near it."*
- Buttons: **Cancel** / **Open System Settings** (no Skip — required to use Sayzo)
- Pre-open body (idle state):
  - *"What Sayzo can do with this: watch for the one shortcut you set, and read the title of your active browser tab (e.g. 'Meet — abc-defg-hij') to detect when you've joined a web meeting. Page contents, what you type into pages, your browsing history — none of it crosses over."*
  - *"Without this on, your shortcut won't work outside the Sayzo window and Sayzo can't auto-prompt for browser meetings — so we won't move you forward until it's set."*
- On Open: deep-link to `x-apple.systempreferences:…Privacy_Accessibility`, screen flips to **Waiting for Accessibility…** state with a pulsing dot. Sayzo is **not** pre-populated in the list (macOS gates the entry behind a manual add), so the follow-up copy walks the user through the +-button flow:
  - *"Sayzo isn't in the Accessibility list yet — macOS keeps that gate locked, so even Sayzo can't toggle it on for you. Add it once and we'll detect it automatically."*
  - Numbered steps: 1) Click the **+** button under the list. 2) Pick **Sayzo** from your Applications folder, then click Open. 3) Toggle **Sayzo** on. We'll spot it within a second or two and unlock Continue.
- Verification: setup window polls `AXIsProcessTrusted()` every 1.5s once the user has clicked Open Settings. The Continue button stays hidden until the call returns True — there is no "trust me, I clicked Allow" advance path.
- Granted state (**APPROVED**): *"All set! Your shortcut works anywhere, and Sayzo can spot browser meetings."*

> **Why no separate Automation screen.** v1 used `osascript` to read browser tab URLs, which forced the macOS Automation TCC dialog (*"Sayzo wants to control your browser"*) — alarming wording for a coaching app, especially around work browsers with sensitive content. v2 reads browser **window titles** via `AXUIElementCopyAttributeValue`, which is gated by the **same** Accessibility permission already required for the global hotkey. No second dialog ever appears. Detection precision is preserved by combining title-regex matching with the existing mic-holder filter (`detectors._browser_holds_mic`).

### Screen 06 — Notifications (both platforms, different copy)

- Title (macOS, **APPROVED**): **Let Sayzo send you notifications**
- Title (Windows, **APPROVED**): **Check your notification settings**
- Body (macOS, **APPROVED**): *"Sayzo asks before recording when it spots you in a meeting, and lets you know when a conversation saves. Skip this and you won't see the ask."*
- Body (Windows, **APPROVED**): *"Sayzo asks before recording when it spots you in a meeting. Make sure notifications are on so the prompts actually show up."*
- Buttons: **Cancel** / **Skip for now** / **Grant** (macOS) or **Check setting** (Windows)
- Pending label: **Asking…** (macOS) / **Checking…** (Windows)
- Sub-body (pre-grant, macOS, **APPROVED**): *"macOS will ask once. Click Allow so you don't miss the meeting prompts."*
- Sub-body (pre-grant, Windows, **APPROVED**): *"Make sure Sayzo is enabled under Settings → System → Notifications."*
- Granted sub-body (**APPROVED**): *"All set."*
- Denied sub-body (**APPROVED**): *"Notifications are blocked. Open Settings to turn them on, then try again."*

### Screen 07 — Shortcut (both)

- Title: **Last thing — pick your shortcut**
- Body (**APPROVED**): *"This is the key you press when you want Sayzo to start or stop a capture. It's the main way you tell Sayzo to record. You can change it anytime from Settings."*
- Field: shortcut capture pill + **Change…** button. While recording: *"Press a key combination… (Esc to cancel)"*.
- Error (no modifier): *"Please include at least one modifier (Ctrl, Alt, Shift, ⌘)."*
- Error (OS-reserved combo): *"That shortcut is used by the OS for {clipboard copy / app switcher / …}. Please pick another."*
- Buttons: **Cancel** / **Continue** (disabled while saving)

### Screen 08 — Done (both)

- Title: **You're all set**
- Body (**APPROVED**): *"Press {hotkey} to start a capture, or say yes when Sayzo spots a meeting. That's it — nothing records until you say so."*
- Sub-body (**APPROVED**): *"Sayzo lives in your menu bar — click it any time to start, stop, or open Settings."*
- Body interpolates the user's chosen shortcut (humanized, e.g. `Ctrl+Alt+S`).
- Button: **Got it** (or **Closing…** while writing markers + finishing).
- Enter also dismisses.
- On click: writes `.permissions_onboarded_v1`, closes the window → service persists `.setup-seen` and registers the launchd agent (macOS) / starts the tray (both).

---

## 3. Toasts — `sayzo_agent/notify.py` + `sayzo_agent/arm/controller.py`

Ordered by when they fire in a typical session.

### 3.1 Welcome (first launch only) — non-interactive

- Title: **Sayzo is running**
- Body: *"Press {hotkey} anytime to start a meeting capture. We'll also ask you when we notice you're in a meeting."*

### 3.2 Consent — whitelist auto-suggest — interactive

Fires when a whitelisted meeting app starts holding the mic and the agent is disarmed.

- Title: **Sayzo is ready to coach you**
- Body: *"Looks like you're in {app name, e.g. Zoom}. Want us to capture this so we can highlight your coachable moments?"*
- Buttons: **Start coaching** / **Not now**
- Timeout: 30 s → **Not now** (sets cooldown)

### 3.3 Start-recording confirmation — hotkey while disarmed — interactive

- Title: **Start recording?**
- Body: *"Sayzo will capture this conversation so we can coach you on it."*
- Buttons: **Yes, start** / **Cancel**
- Timeout: 10 s → **Cancel**

### 3.4 Post-arm guidance — non-interactive

Fires after any successful arm (consent, hotkey, or confirmation accepted).

- Title: **Sayzo is capturing**
- Body: *"Press {hotkey} anytime to stop."*

### 3.5 Stop-recording confirmation — hotkey while armed — interactive

- Title: **Stop recording?**
- Body: *"We'll save what we've captured so far."*
- Buttons: **Yes, stop** / **Keep going**
- Timeout: 10 s → **Keep going**

### 3.6 End-of-meeting confirmation — joint silence → PENDING_CLOSE — interactive

Fires when both sides have been quiet for 45 s.

- Title: **Was that the end of your meeting?**
- Body: *"It's been quiet for a bit. Wrap up and save, or keep going?"*
- Buttons: **Yes, done** / **Not yet**
- Timeout: 15 s → **Yes, done**
- Auto-dismiss if VAD detects speech during the toast.

### 3.7 Long-meeting check-in — elapsed-session mark — interactive

Fires at 1h, 2h, 2h30, 3h, 3h30, every 30 min after.

- Title: **Still in the meeting?**
- Body: *"Sayzo has been capturing for {duration, e.g. 2 hours}. Keep going, or wrap up?"*
- Buttons: **Yes, keep going** / **Wrap up**
- Timeout: 15 s → **Yes, keep going**
- Auto-dismiss if VAD detects speech during the toast.

### 3.8 Meeting-ended watcher — arm-app released mic — interactive

Whitelist-armed sessions only. Fires 15 s after the app that armed us stops holding the mic.

- Title: **Looks like your meeting ended**
- Body: *"Sayzo noticed {app name, e.g. Zoom} stopped using the microphone. Wrap up and save, or keep going?"*
- Buttons: **Wrap up** / **Keep going**
- Timeout: 15 s → **Wrap up**
- On **Keep going**: snooze the watcher 10 min, re-fire if still absent.
- Auto-dismiss if VAD detects speech during the toast.

### 3.9 Capture saved — non-interactive

- Title: **Conversation saved**
- Body: *"{verdict.title} · {duration}"*

### 3.10 Stream-open error — non-interactive

Fires when the mic or loopback device can't be opened on arm.

- Title: **Couldn't start capturing**
- Body: *"Sayzo couldn't access your microphone or speakers. Try closing other recording apps, then press {hotkey} again."*

---

## 4. Tray menu labels

### When disarmed
- Top item (**APPROVED**): **Start recording   ({hotkey})**
- **Settings...**
- **Open captures folder**
- **Quit Sayzo**

### When armed
- Top item: **Stop recording   ({hotkey})**
- **Settings...**
- **Open captures folder**
- **Quit Sayzo**

### Tooltip
- Disarmed (**APPROVED**): *"Sayzo — mic off. Press {hotkey} to start, or we'll ask when you're in a meeting."*
- Armed: *"Sayzo — capturing. Press {hotkey} to stop."*

"Reopen setup" was removed — post-setup tweaks happen in **Settings…**, which already exposes the shortcut picker and macOS permission re-requests.

---

## 5. Settings window labels

### Sidebar
- Shortcut
- Permissions *(macOS only; on Windows this section is a short "no permissions required" note)*
- Account
- Notifications

### Shortcut panel
- Header: **Start-recording shortcut**
- Subtext: *"Press this anywhere on your computer to start a capture, or stop one in progress."*
- Field label: **Current shortcut**
- Button label: **Change...**
- Capture mode placeholder: *"Press a key combination... (Esc to cancel)"*
- Save button: **Save**
- Conflict error: *"That shortcut is already in use by another app. Try a different combination."*
- Missing-modifier error: *"Please include at least one modifier (Ctrl, Alt, Shift, ⌘)."*

### Permissions panel
- Header: **Permissions**
- Row labels (and status): **Microphone**, **System Audio Recording**, **Accessibility**, **Automation (browsers)**
- Status values: `✓ Granted`, `✗ Denied`, `— Not requested yet`
- Re-request button: **Re-request**

### Account panel (signed-in)
- Header: **Account**
- Body (**APPROVED**): *"Signed in. Your captures sync to your account so you can drill the coaching moments in the Sayzo web app."*
- Row 1: **Server: [server_url]**
- Row 2: **Signed in since [date]**
- Button: **Open webapp**
- Button: **Sign out**

### Account panel (signed-out)
- Header: **Account**
- Body (**APPROVED**): *"You're not signed in. Sayzo will keep captures on this machine until you do — so no coaching drills yet."*
- Button: **Sign in**

### Notifications panel
- Header: **Notifications**
- Master toggle: **Show Sayzo notifications**
- Sub-toggle: **Show the welcome message on first launch**
- Sub-toggle: **Show "Sayzo is capturing" reminders after I arm**
- Sub-toggle: **Show "Conversation saved" when a capture finishes**
- Subtext (below the sub-toggles): *"Consent prompts and end-of-meeting questions always show — they're how you decide what Sayzo captures."*

---

## 6. macOS Info.plist usage descriptions — `sayzo-agent.spec`

These are the strings macOS shows in its own native TCC permission dialogs. They appear the first time the OS prompt fires (alongside the in-app explanation in the pywebview). Must match the armed-only invariant — no "always listening" language.

### `NSMicrophoneUsageDescription` (**APPROVED**)

> Sayzo opens the microphone only when you start a recording. It stays off otherwise.

### `NSAudioCaptureUsageDescription` (**APPROVED**)

> So Sayzo can hear the other person in your meetings (Zoom, Meet, FaceTime, etc.) — only while you're recording.

### `NSAppleEventsUsageDescription` (**APPROVED**)

> So Sayzo can tell when you're in a web meeting (Google Meet, Teams, etc.). Only the tab's URL — never what's on the page.

*Corrects the previous version, which incorrectly said AppleEvents was used for opening System Settings. Deep-linking to Settings uses `open x-apple.systempreferences:…` and doesn't trigger the AppleEvents TCC prompt; browser-tab-URL reading is the actual use.*

---

## 7. Heartbeat log lines (terminal only — not user-facing but still you-facing)

Not strictly copy, but the log format changes with the armed model. Included here for review.

- Disarmed: `[heartbeat] state=DISARMED waiting for hotkey or meeting detect llm=unloaded kept=0 discarded=0`
- Armed, no session yet: `[heartbeat] state=ARMED (hotkey) pre_buffer mic=0.0s sys=0.0s llm=unloaded kept=0 discarded=0`
- Armed, session open: `[heartbeat] state=ARMED (zoom) OPEN elapsed=12.3s mic_voiced=3.1s sys_voiced=8.2s llm=unloaded kept=0 discarded=0`
- Pending close: `[heartbeat] state=ARMED (zoom) PENDING_CLOSE elapsed=67.3s silence=47.2s llm=unloaded kept=0 discarded=0`
- Startup line: `[agent] running. Shortcut: Ctrl+Alt+S. Ctrl+C to stop.`

---

## Review workflow

1. Read each block. Mark **APPROVED** in a comment, or rewrite in place.
2. Ping me when you've gone through it.
3. I'll encode the approved versions into NSIS / React / toast templates.
4. Any block left without APPROVED stays as-is here; I won't ship it into the installer/UI until you've signed off.
