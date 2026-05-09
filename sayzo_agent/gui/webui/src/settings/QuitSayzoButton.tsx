import { useEffect, useState } from "react";
import { settingsBridge } from "../lib/settings-bridge";
import { Button } from "../components/ui/Button";

// Sidebar-footer entry that fully shuts down the agent. Closing the
// Settings window only hides this view — the agent keeps running.
export function QuitSayzoButton() {
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function openConfirm() {
    setError(null);
    setConfirming(true);
  }

  function closeConfirm() {
    if (pending) return;
    setConfirming(false);
  }

  async function handleQuit() {
    setPending(true);
    setError(null);
    try {
      const result = await settingsBridge.quitAgent();
      if (!result.ok) {
        // Agent unreachable — close locally so the user gets feedback.
        window.close();
        return;
      }
      // Agent shutdown will kill this Settings subprocess within ~500 ms;
      // leave "Quitting…" on screen until then so there's no flicker.
    } catch (e) {
      setError(String(e));
      setPending(false);
    }
  }

  return (
    <>
      <Button
        variant="secondary"
        className="w-full justify-center border-red-200 text-red-700 hover:bg-red-50"
        onClick={openConfirm}
      >
        Quit Sayzo
      </Button>

      {confirming && (
        <ConfirmModal
          onCancel={closeConfirm}
          onConfirm={handleQuit}
          pending={pending}
          error={error}
        />
      )}
    </>
  );
}

interface ConfirmModalProps {
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
  pending: boolean;
  error: string | null;
}

function ConfirmModal({
  onCancel,
  onConfirm,
  pending,
  error,
}: ConfirmModalProps) {
  // Escape closes the modal. Mirrors AddAppDialog so keyboard behaviour
  // is consistent across Settings dialogs.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !pending) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, pending]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="quit-sayzo-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget && !pending) onCancel();
      }}
    >
      <div className="w-full max-w-md overflow-hidden rounded-lg bg-white shadow-xl">
        <div className="px-7 pt-6 pb-2">
          <h2
            id="quit-sayzo-title"
            className="text-lg font-semibold tracking-tight text-ink"
          >
            Quit Sayzo?
          </h2>
        </div>
        <div className="space-y-3 px-7 pb-2 text-sm leading-relaxed text-ink-muted">
          <p>
            This stops Sayzo completely — it won't detect or capture
            meetings until you launch it again.
          </p>
          <p>
            If you only wanted to close this window, click Cancel and use
            the close button instead — Sayzo will keep running.
          </p>
        </div>
        {error && (
          <div className="mx-7 mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900">
            Couldn't quit Sayzo: {error}
          </div>
        )}
        <div className="flex items-center justify-end gap-2 border-t border-ink-border px-7 py-4 mt-4">
          <Button variant="secondary" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            className="bg-red-600 hover:bg-red-700 focus-visible:ring-red-300"
            onClick={onConfirm}
            disabled={pending}
          >
            {pending ? "Quitting…" : "Quit Sayzo"}
          </Button>
        </div>
      </div>
    </div>
  );
}
