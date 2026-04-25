import { useEffect, useState } from "react";
import { settingsBridge, PermissionRow } from "../lib/settings-bridge";
import { Button } from "../components/ui/Button";

export function PermissionsPane() {
  const [rows, setRows] = useState<PermissionRow[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await settingsBridge.getPermissions();
        if (!cancelled) setRows(r);
      } catch {
        if (!cancelled) setRows([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleReRequest(key: string) {
    // Try the programmatic prompt first. mic + audio_capture surface a TCC
    // dialog directly; accessibility + automation return granted=null so we
    // fall through to the deep-link.
    try {
      const result = await settingsBridge.requestPermission(key);
      if (result.granted === null) {
        await settingsBridge.openPermissionSettings(key);
      }
    } catch {
      // Best-effort fallback to the deep-link if the prompt path threw.
      try {
        await settingsBridge.openPermissionSettings(key);
      } catch {
        // Nothing else to do — surface failure silently; the user can
        // retry from the same button.
      }
    }
  }

  if (rows == null) {
    return (
      <div className="text-sm text-ink-muted">Loading permissions…</div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Permissions
      </h1>

      {rows.length === 0 ? (
        <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
          Sayzo doesn't need any special permissions on Windows. If
          notifications aren't showing, check Windows Settings → System →
          Notifications.
        </p>
      ) : (
        <>
          <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
            Grant the permissions Sayzo needs to capture meetings and let
            the keyboard shortcut work anywhere.
          </p>

          <div className="mt-8 space-y-6">
            {rows.map((row) => (
              <PermissionRowView
                key={row.key}
                row={row}
                onReRequest={() => handleReRequest(row.key)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

interface RowProps {
  row: PermissionRow;
  onReRequest: () => void;
}

function PermissionRowView({ row, onReRequest }: RowProps) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex-1">
        <div className="text-sm font-medium text-ink">{row.label}</div>
        <div className="mt-1 text-xs leading-relaxed text-ink-muted">
          {row.description}
        </div>
      </div>
      <Button variant="secondary" onClick={onReRequest}>
        Re-request
      </Button>
    </div>
  );
}
