# User-facing copy — draft for review

All strings that end up in front of a user live here first. Review and revise *before* they get encoded into the NSIS script, onboarding UI, or toast templates. Nothing in this file is shipped as-is; it's the staging area.

When you're happy with a block, mark it **APPROVED** in a comment next to it, and I'll propagate the approved text into the actual code/installer paths.

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
> We'll open a quick setup window so you can pick your start-recording shortcut. You can change it anytime from the Sayzo tray menu.

---

## 2. Windows first-run welcome window (Python) — `sayzo_agent/onboarding.py`

Runs after NSIS finishes. Two pages. Re-uses the Settings window widget for hotkey capture.

### Page 1 — Shortcut

- Title: **Pick your start-recording shortcut**
- Body: *"Press this anywhere on your computer to start capturing a meeting, or to stop a capture in progress. You can change it later in Sayzo's settings."*
- Field: shortcut capture widget, prefilled with `Ctrl+Alt+S`.
- Button: **Looks good →**

### Page 2 — Done

- Title: **You're all set**
- Body: *"Sayzo is running in your system tray. Press **[their chosen hotkey]** anytime to start capturing a meeting. We'll also ask you when we notice you're in a meeting app like Zoom, Teams, Discord, or Google Meet."*
- Button: **Got it**

---

## 3. macOS onboarding walkthrough (Python) — `sayzo_agent/onboarding.py`

Five steps. First four are permissions; fifth is the hotkey picker.

### Step 1 — Microphone access

- Title: **Sayzo needs to hear you**
- Body: *"We'll only record when you ask — either with your keyboard shortcut, or after you say yes to an on-screen prompt. This permission just lets Sayzo open the microphone at that moment."*
- Buttons: **Grant** / **Skip for now**
- On Grant: triggers `AVCaptureDevice.requestAccess(for: .audio)` → OS prompt.

### Step 2 — System Audio Recording

- Title: **And the other side of your meetings**
- Body: *"So Sayzo can transcribe what the other person says, not just you. We don't read or record anything visible on your screen — just audio coming through your speakers."*
- Buttons: **Grant** / **Skip for now**
- On Grant: triggers whatever surfaces the TCC prompt for CoreAudio Process Taps on the target macOS version. Needs real-Mac verification.

### Step 3 — Accessibility

- Title: **Let the shortcut work anywhere**
- Body: *"Without this, Sayzo's global shortcut won't work when another app is focused. You'll have to open the tray menu to start recording. You can grant this later from System Settings."*
- Buttons: **Open System Settings** / **Skip for now**
- On Grant: opens `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`.

### Step 4 — Automation (Chrome / Safari / Edge)

- Title: **Know when you're in a web meeting**
- Body: *"So Sayzo can tell you're in Google Meet or Teams on the web, instead of just browsing. We only read the current tab's URL — never its contents."*
- Buttons: **Grant (per browser)** / **Skip for now**
- On Grant: runs a throwaway `tell application "Google Chrome" to get URL of active tab of front window` for each installed browser, surfacing the OS prompt.

### Step 5 — Pick your start-recording shortcut

- Title: **Last thing — pick your shortcut**
- Body: *"Press this anywhere to start capturing a meeting, or stop one in progress. You can change it anytime in Sayzo's settings."*
- Field: shortcut capture widget, prefilled with `Ctrl+Alt+S`.
- Button: **Done — take me to the tray**

### Re-open copy (when user clicks "Reopen setup" in tray)

Same pages, same copy. No special "welcome back" language.

---

## 4. Toasts — `sayzo_agent/notify.py` + `sayzo_agent/arm/controller.py`

Ordered by when they fire in a typical session.

### 4.1 Welcome (first launch only) — non-interactive

- Title: **Sayzo is running**
- Body: *"Press **[hotkey]** anytime to start a meeting capture. We'll also ask you when we notice you're in a meeting."*

### 4.2 Consent — whitelist auto-suggest — interactive

Fires when a whitelisted meeting app starts holding the mic and the agent is disarmed.

- Title: **Sayzo is ready to coach you**
- Body: *"Looks like you're in **[app name, e.g. Zoom]**. Want us to capture this so we can highlight your coachable moments?"*
- Buttons: **Start coaching** / **Not now**
- Timeout: 30 s → **Not now** (sets cooldown)

### 4.3 Start-recording confirmation — hotkey while disarmed — interactive

- Title: **Start recording?**
- Body: *"Sayzo will capture this conversation so we can coach you on it."*
- Buttons: **Yes, start** / **Cancel**
- Timeout: 10 s → **Cancel**

### 4.4 Post-arm guidance — non-interactive

Fires after any successful arm (consent, hotkey, or confirmation accepted).

- Title: **Sayzo is capturing**
- Body: *"Press **[hotkey]** anytime to stop."*

### 4.5 Stop-recording confirmation — hotkey while armed — interactive

- Title: **Stop recording?**
- Body: *"We'll save what we've captured so far."*
- Buttons: **Yes, stop** / **Keep going**
- Timeout: 10 s → **Keep going**

### 4.6 End-of-meeting confirmation — joint silence → PENDING_CLOSE — interactive

Fires when both sides have been quiet for 45 s.

- Title: **Was that the end of your meeting?**
- Body: *"It's been quiet for a bit. Wrap up and save, or keep going?"*
- Buttons: **Yes, done** / **Not yet**
- Timeout: 15 s → **Yes, done**
- Auto-dismiss if VAD detects speech during the toast.

### 4.7 Long-meeting check-in — elapsed-session mark — interactive

Fires at 1h, 2h, 2h30, 3h, 3h30, every 30 min after.

- Title: **Still in the meeting?**
- Body: *"Sayzo has been capturing for **[duration, e.g. 2 hours]**. Keep going, or wrap up?"*
- Buttons: **Yes, keep going** / **Wrap up**
- Timeout: 15 s → **Yes, keep going**
- Auto-dismiss if VAD detects speech during the toast.

### 4.8 Meeting-ended watcher — arm-app released mic — interactive

Whitelist-armed sessions only. Fires 15 s after the app that armed us stops holding the mic.

- Title: **Looks like your meeting ended**
- Body: *"Sayzo noticed **[app name, e.g. Zoom]** stopped using the microphone. Wrap up and save, or keep going?"*
- Buttons: **Wrap up** / **Keep going**
- Timeout: 15 s → **Wrap up**
- On **Keep going**: snooze the watcher 10 min, re-fire if still absent.
- Auto-dismiss if VAD detects speech during the toast.

### 4.9 Capture saved — existing, non-interactive

Unchanged from current behavior. Shown after sink writes.

- Title: **Conversation saved**
- Body: *"**[verdict.title]** · **[duration]**"*

### 4.10 Stream-open error — non-interactive

Fires when the mic or loopback device can't be opened on arm.

- Title: **Couldn't start capturing**
- Body: *"Sayzo couldn't access your microphone or speakers. Try closing other recording apps, then press **[hotkey]** again."*

---

## 5. Tray menu labels

### When disarmed
- Top item: **Arm Sayzo   ([hotkey])**
- **Settings...**
- **Reopen setup**
- **Quit Sayzo**

### When armed
- Top item: **Stop recording   ([hotkey])**
- **Settings...**
- **Reopen setup**
- **Quit Sayzo**

### Tooltip
- Disarmed: *"Sayzo — idle. Press [hotkey] or join a meeting."*
- Armed: *"Sayzo — capturing. Press [hotkey] to stop."*

---

## 6. Settings window labels

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
- Walkthrough button: **Reopen setup walkthrough**

### Account panel (signed-in)
- Header: **Account**
- Row 1: **Signed in as [email]**
- Row 2: **Server: [server_url]**
- Row 3: **Signed in since [date]**
- Button: **Open webapp**
- Button: **Sign out**

### Account panel (signed-out)
- Header: **Account**
- Body: *"You're not signed in. Sayzo will keep captures locally, but won't sync them to the webapp until you sign in."*
- Button: **Sign in**

### Notifications panel
- Header: **Notifications**
- Master toggle: **Show Sayzo notifications**
- Sub-toggle: **Show the welcome message on first launch**
- Sub-toggle: **Show "Sayzo is capturing" reminders after I arm**
- Sub-toggle: **Show "Conversation saved" when a capture finishes**
- Subtext (below the sub-toggles): *"Consent prompts and end-of-meeting questions always show — they're how you decide what Sayzo captures."*

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
3. I'll encode the approved versions into NSIS / Python / toast templates and mark task #13 done.
4. Any block left without APPROVED stays as-is here; I won't ship it into the installer/UI until you've signed off.
