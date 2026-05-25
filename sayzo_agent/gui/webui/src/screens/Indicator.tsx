import { useEffect, useState } from "react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";
import indicatorVisibleMp4 from "../assets/indicator-visible.mp4";
import indicatorHiddenMp4 from "../assets/indicator-hidden.mp4";

interface Props {
    step: string;
    onNext: () => void;
    onCancel: () => void;
}

type Choice = "visible" | "hidden";

const COPY: Record<Choice, { label: string; caption: string }> = {
    visible: {
        label: "Show indicator",
        caption:
            "A small reminder sits in the corner while Sayzo records. Click it any time to stop.",
    },
    hidden: {
        label: "Stay out of the way",
        // MUST keep the "still records" reassurance — the highest-risk copy
        // failure is a user picking this thinking it disables recording.
        caption:
            "No floating reminder. Sayzo still records — your tray icon shows it's running, and you can stop from there.",
    },
};

// Onboarding picker for the recording indicator (HUD pill). Sits between
// the Shortcut step and the Done step. Persists the chosen visibility to
// `cfg.hud.show_recording_indicator` via the setup bridge — gated at the
// single `show_pill()` call site in `arm/controller.py::_arm_internal`.
//
// Layout: one preview hero + two slim selector pills + a single caption,
// all sharing one centered ~400px column so every edge lines up (an
// earlier pass centered a narrow hero over a wider chip row, which read as
// misaligned). The hero spans the full group width; the two clips stay
// mounted and cross-fade on selection (no <video> remount / reload flash).
// The setup window is sized (in gui/setup/window.py) so this — the tallest
// step — doesn't scroll.
//
// "Visible" is the default selection: it preserves current production
// behaviour for upgraders, gives new users a live trust signal during
// their first arm, and the Visible -> Hidden discovery path (find it
// intrusive, open Settings -> Recording, switch) is symmetric with the
// reverse, which isn't.
export function Indicator({ step, onNext, onCancel }: Props) {
    const [choice, setChoice] = useState<Choice>("visible");
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        // Re-entry hydrate: a user who quit setup mid-way and re-opened gets
        // their previous pick pre-selected. New users see "Visible" since
        // that's the field default in HudConfig.
        let cancelled = false;
        void bridge
            .getRecordingIndicator()
            .then((r) => {
                if (!cancelled) setChoice(r.visible ? "visible" : "hidden");
            })
            .catch(() => {
                // Bridge unavailable in dev / mock — keep the "visible" default.
            });
        return () => {
            cancelled = true;
        };
    }, []);

    async function handleContinue() {
        if (saving) return;
        setSaving(true);
        try {
            await bridge.setRecordingIndicator(choice === "visible");
        } catch {
            // Persist failures are silent here — the in-process cfg default of
            // True still lines up with the visible-side option, so a failed
            // save on the Hidden choice is the only divergence. Advancing
            // anyway is the right call: blocking onboarding on a Settings
            // write that will likely succeed next time is worse than the user
            // re-toggling in Settings if the value drifted.
        } finally {
            setSaving(false);
            onNext();
        }
    }

    return (
        <Layout
            step={step}
            title="How do you want to see Sayzo while it records?"
            subtitle="Both options record exactly the same — you can change this anytime in Settings."
            footer={
                <>
                    <Button
                        variant="ghost"
                        onClick={onCancel}
                        disabled={saving}
                    >
                        Cancel
                    </Button>
                    <Button onClick={handleContinue} disabled={saving}>
                        {saving ? "Saving…" : "Continue"}
                    </Button>
                </>
            }
        >
            {/* Hero + selectors share one centered column so their edges align.
          The hero box is exactly 16:9 (matches the 1024x576 source), so
          object-cover shows the full frame with no crop. */}
            <div className="w-full">
                <div className="relative aspect-video w-full overflow-hidden rounded-xl border border-ink-border bg-ink/5 shadow-sm">
                    <video
                        src={indicatorVisibleMp4}
                        aria-hidden="true"
                        className={`absolute inset-0 h-full w-full object-cover transition-opacity duration-300 ${
                            choice === "visible" ? "opacity-100" : "opacity-0"
                        }`}
                        autoPlay
                        muted
                        loop
                        playsInline
                    />
                    <video
                        src={indicatorHiddenMp4}
                        aria-hidden="true"
                        className={`absolute inset-0 h-full w-full object-cover transition-opacity duration-300 ${
                            choice === "hidden" ? "opacity-100" : "opacity-0"
                        }`}
                        autoPlay
                        muted
                        loop
                        playsInline
                    />
                </div>

                <div
                    role="radiogroup"
                    aria-label="Recording indicator visibility"
                    className="mt-4 grid grid-cols-2 gap-2.5"
                >
                    <OptionPill
                        label={COPY.visible.label}
                        selected={choice === "visible"}
                        onSelect={() => setChoice("visible")}
                    />
                    <OptionPill
                        label={COPY.hidden.label}
                        selected={choice === "hidden"}
                        onSelect={() => setChoice("hidden")}
                    />
                </div>

                {/* Dynamic caption — describes the selected option. min-h reserves
            space for the longer (hidden) caption so selecting doesn't
            shift the layout. */}
                <p className="mt-3 min-h-[2.75rem] text-center text-xs leading-relaxed text-ink-muted">
                    {COPY[choice].caption}
                </p>
            </div>
        </Layout>
    );
}

interface OptionPillProps {
    label: string;
    selected: boolean;
    onSelect: () => void;
}

function OptionPill({ label, selected, onSelect }: OptionPillProps) {
    const tone = selected
        ? "border-accent ring-2 ring-accent/30 text-ink"
        : "border-ink-border text-ink-muted hover:border-ink/40 hover:text-ink";
    return (
        <button
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={onSelect}
            className={`flex items-center justify-center gap-2 rounded-lg border bg-white px-3 py-2.5 text-sm font-medium transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${tone}`}
        >
            <span
                className={`grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border ${
                    selected ? "border-accent" : "border-ink-border"
                }`}
            >
                {selected && (
                    <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                )}
            </span>
            {label}
        </button>
    );
}
