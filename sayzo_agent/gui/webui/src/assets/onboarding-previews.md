# Onboarding "Indicator" step previews

The Indicator onboarding screen (`screens/Indicator.tsx`) shows users what
the **Visible** vs **Hidden** recording indicator looks like in practice.
Both MP4 loops are recorded and committed. Re-record from this brief if the
indicator's look or the step's copy changes.

The screen renders ONE large hero preview that cross-fades between the two
clips as the user toggles the choice chips beneath it (not two side-by-side
cards — that layout squeezed each video too small inside Layout's narrow
column). Behind the video sits a faint panel (`bg-ink/5`), so a missing /
invalid MP4 degrades to an empty tinted box rather than a broken-image
icon; the choice still persists.

## Spec

- Container: `.mp4` (H.264) — emitted by ScreenToGif's "System Encoder
  (Mp4)" export or `ffmpeg -c:v libx264`.
- Dimensions: **1024×576 px (16:9)** — fills the hero's `aspect-video`
  frame exactly, so `object-cover` shows the whole frame with no crop. Any
  16:9 size works (1280×720 source cropped/scaled to 1024×576 is what
  ships); off-ratio clips get center-cropped to 16:9.
- Length: 5–7 s. Loop point: a clean fade or cut back to the opening
  frame so the autoplay loop feels continuous.
- Audio: **none**. Strip with `-an` in ffmpeg or "Remove audio" in
  ScreenToGif before export.
- Frame rate: ~24 fps is fine.
- Target file size: ≤300 KB each (the React `<video>` tag streams them
  from the dist bundle on every onboarding load — bigger = slower first
  paint of the step).

Use the same machine, wallpaper, and meeting-app window position for both
clips so the only meaningful difference between the cards is whether the
indicator appears.

## Tooling

- **Windows**: ScreenToGif (free — https://www.screentogif.com/). Despite
  the name, export as MP4 via *Save As → System Encoder (Mp4)*. Use the
  Cursor toggle in record options to hide the pointer.
- **macOS**: Cmd+Shift+5 → record area → save as `.mov` → compress:
  `ffmpeg -i recording.mov -vf scale=1024:576 -c:v libx264 -crf 28 -an indicator-visible.mp4`

## Choreography — `indicator-visible.mp4`

Setup: Zoom (or any meeting app) in a windowed (not maximized) view with
the camera off. Record a 16:9 region (crop / scale to 1024×576 on export)
of the top-right corner — include the spot where the floating pill appears
plus enough of the Zoom window that it's recognizable as a meeting.

1. **0.0 s** — Zoom window only. No Sayzo UI.
2. **1.0 s** — Press Ctrl+Alt+S. Start-confirmation toast animates in.
3. **2.0 s** — Click *Yes* (or press Y).
4. **3.0 s** — Toast dismisses. The pill animates in: logo + waveform
   bars + "Done" button.
5. **3.5–5.0 s** — Speak briefly so the waveform pulses (the clip is
   muted — the visual pulse is the point).
6. **5.5 s** — Cut. The editor's fade-back-to-start makes the autoplay
   loop read as one continuous "Sayzo armed → indicator appeared".

## Choreography — `indicator-hidden.mp4`

Same setup, same area. Make sure the tray icon (bottom-right on Windows)
or menu bar icon (top-right on macOS) is visible at the edge of the
frame — it's the silent star of this clip.

To record this clip before the gate code lands you'll need to
temporarily suppress the pill yourself; pick one:

- Set the env var **`SAYZO_HUD__SHOW_RECORDING_INDICATOR=0`** before
  launching Sayzo.
- Or, after the feature ships, open Settings → Recording and toggle
  "Show recording indicator" off.

Then:

1. **0.0 s** — Zoom window foreground. Tray / menu-bar icon visible at
   the edge.
2. **1.0 s** — Press Ctrl+Alt+S. Start-confirmation toast animates in.
3. **2.0 s** — Click *Yes*.
4. **3.0 s** — Toast dismisses. **No pill appears.** Briefly hover the
   tray icon (don't click — just hover) so its tooltip / menu label
   surfaces. On Windows the menu reads "Stop recording (Ctrl+Alt+S)";
   on macOS the menu bar icon's title is "Sayzo".
5. **4.0–6.0 s** — Hold on the unchanged Zoom window for ~2 seconds. The
   whole point of this option is "your workspace continues uninterrupted."
6. **6.5 s** — Cut and loop.
