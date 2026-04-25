import { useState } from "react";

// Shared "click → copy → 'Copied!' for 2s → revert" hook. Used by every
// pane that exposes a Copy button (About's diagnostics, Account's login URL,
// future panes' shortcut display, etc.). Centralising here means the timeout
// constant and the silent-fallback behaviour stay consistent.
//
// Clipboard access can fail in obscure pywebview backend states; the hook
// resolves quietly and leaves `copied` false rather than throwing.
export function useCopyToClipboard(resetMs = 2000) {
  const [copied, setCopied] = useState(false);

  async function copy(text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), resetMs);
    } catch {
      // Swallow — the user can re-trigger from the same button.
    }
  }

  return { copied, copy };
}
