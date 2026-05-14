# Verify the VAD-timestamp refactor (v2.18) locally — no GitHub build needed

The point of this recipe: get high confidence that the refactor is
behavior-neutral against **production code paths**, without waiting on
CI. The order goes from fastest+cheapest to slowest+strongest.

The trap to avoid: don't rely on the `sayzo-agent replay …` command as
your sole signal. v2.17 burned us on exactly that — the replay path has
its own teardown wiring that masked the production-only flush-on-close
bug. Layers 1–3 below all drive `ArmController` directly.

---

## Layer 1 — pytest unit suite (≈ 45 s)

```powershell
.venv\Scripts\python.exe -m pytest tests\ --ignore=tests\test_whitelist_helpers.py -q
```

Expected: **723 passed, 7 skipped**. Same as v2.17 baseline. Any new
failure is a regression — the refactor's contract drifted somewhere.

If you want just the new tests:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_vad_timestamps.py tests\test_arm_controller.py::test_arm_cycle_preserves_mono_timestamp_through_to_session -v
```

## Layer 2 — production-path probes (< 5 s each)

Three probes, all drive `ArmController._on_hotkey_pressed` (the
production path) with fake captures + scriptable fake VADs:

```powershell
# VAD-flush-on-close still works (the v2.17 bug):
.venv\Scripts\python.exe scripts\probe_vad_flush.py

# Monotonic timestamps round-trip through the rebase seam (v2.18 contract):
.venv\Scripts\python.exe scripts\probe_vad_timestamps.py

# The original v2.17 bug-class scenarios (multi-arm, mid-utterance close,
# cross-arm anchoring) all still hold:
.venv\Scripts\python.exe scripts\probe_v17_scenarios.py
```

Each prints a list of `[PASS]` / `[FAIL]` lines and a final `Result:
PASS` or `Result: FAIL` line. Run all three; all should PASS.

## Layer 3 — live arm + speak on the user's machine (no build)

The most production-faithful local verification. `sayzo-agent run` runs
the source tree directly — no compile, no installer, no GitHub. Real
SileroVAD, real audio devices, real `ArmController`.

1. Open a terminal in the repo:

   ```powershell
   sayzo-agent run
   ```

2. Sign in if prompted (only needed once; capture-saved toasts depend
   on it).

3. Press the hotkey (default `Ctrl+Alt+S`) to arm.

4. Speak three distinct phrases with ~2 s pauses between them. For example:
   - "This is the first phrase."
   - (pause ~2 s)
   - "Now I am speaking the second phrase."
   - (pause ~2 s)
   - "And finally a third phrase that is a bit longer."

5. Press the hotkey again to stop. Confirm the stop toast.

6. Wait for the "Conversation saved" toast.

7. Open the latest capture's `record.json`:

   ```powershell
   $latest = (Get-ChildItem "$env:USERPROFILE\.sayzo\agent\captures" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
   Get-Content "$latest\record.json" | ConvertFrom-Json | Select-Object -ExpandProperty mic_segments | Format-Table
   ```

8. Verify:
   - Three (or so) entries — VAD may split one phrase into two if you
     paused mid-sentence.
   - `start_ts` of each segment is monotonically increasing.
   - `start_ts` of the first segment is roughly 0–2 seconds (the time
     from arm to first word).
   - `end_ts` of the last segment is close to the elapsed time you
     stopped at.

If the numbers look wildly off (e.g. all zeros, all the same, or
negative), the refactor has a bug in the rebase math. Capture the
output and revert.

## Layer 4 (optional) — A/B parity against v2.17

If you want a paper trail showing behavior-neutrality:

1. On the refactor branch, do a Layer 3 run. Save the `record.json` aside.
2. `git stash` (if you have uncommitted work), then `git checkout main`
   or the v2.17 tag.
3. Repeat Layer 3 with the same arm-and-speak scenario.
4. Compare the two `mic_segments` lists. Differences should be within
   one `SILERO_CHUNK / SAMPLE_RATE` ≈ 32 ms (the chunk-grain of VAD
   decisions).
5. `git checkout <refactor-branch>`, `git stash pop`.

Skip this if you trust Layers 1–3. The optional pass is for paranoia.

---

## What "PASS" looks like end-to-end

- Layer 1: `723 passed, 7 skipped`.
- Layer 2: three probes, every line `[PASS]`, three `Result: PASS`.
- Layer 3: `record.json` has three (or three-ish) `mic_segments` with
  sensible monotonically increasing `start_ts` values that roughly
  match what you spoke.

If all three layers pass, the refactor is good to ship without waiting
on the GitHub build.
