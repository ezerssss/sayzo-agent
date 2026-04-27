import { useEffect, useState } from "react";
import { settingsBridge } from "../lib/settings-bridge";
import { Alert } from "../components/ui/Alert";
import { AccountPane } from "./AccountPane";
import { AboutPane } from "./AboutPane";
import { CapturesPane } from "./CapturesPane";
import { MeetingAppsPane } from "./MeetingAppsPane";
import { NotificationsPane } from "./NotificationsPane";
import { PermissionsPane } from "./PermissionsPane";
import { ShortcutPane } from "./ShortcutPane";
import logoUrl from "../assets/logo.png";

// Sidebar order matches the legacy tkinter Settings so muscle memory
// carries over. "Captures" sits next to Meeting Apps since both deal with
// what Sayzo records.
const PANE_NAMES = [
  "Shortcut",
  "Meeting Apps",
  "Captures",
  "Permissions",
  "Account",
  "Notifications",
  "About",
] as const;
type PaneName = (typeof PANE_NAMES)[number];

function normalizePane(s: string | null | undefined): PaneName | null {
  if (s == null) return null;
  const lower = s.toLowerCase();
  return PANE_NAMES.find((n) => n.toLowerCase() === lower) ?? null;
}

const SIDEBAR_REFRESH_MS = 5_000;

export function SettingsApp() {
  const [active, setActive] = useState<PaneName>("Shortcut");
  const [ready, setReady] = useState(false);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [inProgressCount, setInProgressCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // 1) #pane= URL fragment (set by gui/settings/window.py at spawn
        //    time when the parent process passed --pane on the CLI). Lives
        //    in the hash, not the search string — see main.tsx for why.
        const params = new URLSearchParams(
          window.location.hash.replace(/^#/, ""),
        );
        const fromUrl = normalizePane(params.get("pane"));

        // 2) get_initial_pane() — the same value, but routed through the
        //    bridge so a future caller that doesn't know about the URL
        //    contract still works. We prefer the URL on the chance the
        //    bridge hasn't populated initial_pane yet.
        let pane: PaneName | null = fromUrl;
        if (pane == null) {
          const fromBridge = await settingsBridge.getInitialPane();
          pane = normalizePane(fromBridge);
        }

        if (cancelled) return;
        if (pane != null) setActive(pane);
        setReady(true);
      } catch (e) {
        if (!cancelled) setGlobalError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll the in-progress count for the sidebar dot. Cheap (one IPC call +
  // dir scan) and the result is just a number, so we keep it separate from
  // CapturesPane's own polling instead of plumbing shared state.
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const list = await settingsBridge.listCaptures();
        if (cancelled) return;
        const count = list.filter((c) => c.bucket === "in_progress").length;
        setInProgressCount(count);
      } catch {
        // Silent — the user already sees the empty/error state inside the
        // Captures pane if listing is broken; no need to double-notify.
      }
    };
    void refresh();
    const id = window.setInterval(() => {
      void refresh();
    }, SIDEBAR_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (globalError) {
    return (
      <div className="p-10">
        <Alert>
          <div>
            <strong>Couldn't open Settings.</strong> {globalError}
          </div>
        </Alert>
      </div>
    );
  }

  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-muted">
        Loading…
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <Sidebar
        active={active}
        onSelect={setActive}
        inProgressCount={inProgressCount}
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl px-10 py-10">
          {active === "Shortcut" && <ShortcutPane />}
          {active === "Meeting Apps" && <MeetingAppsPane />}
          {active === "Captures" && <CapturesPane />}
          {active === "Permissions" && <PermissionsPane />}
          {active === "Account" && <AccountPane />}
          {active === "Notifications" && <NotificationsPane />}
          {active === "About" && <AboutPane />}
        </div>
      </div>
    </div>
  );
}

interface SidebarProps {
  active: PaneName;
  onSelect: (p: PaneName) => void;
  inProgressCount: number;
}

function Sidebar({ active, onSelect, inProgressCount }: SidebarProps) {
  return (
    <nav className="w-56 shrink-0 border-r border-ink-border bg-gray-50">
      <div className="flex items-center gap-3 px-5 pt-8 pb-6">
        <img src={logoUrl} alt="" className="h-9 w-9 shrink-0" />
        <div className="min-w-0">
          <div className="text-base font-semibold leading-tight tracking-tight text-ink">
            Sayzo
          </div>
          <div className="text-xs text-ink-muted">Settings</div>
        </div>
      </div>
      <ul className="px-2">
        {PANE_NAMES.map((name) => (
          <SidebarItem
            key={name}
            label={name}
            selected={active === name}
            onClick={() => onSelect(name)}
            inProgress={name === "Captures" && inProgressCount > 0}
          />
        ))}
      </ul>
    </nav>
  );
}

interface SidebarItemProps {
  label: string;
  selected: boolean;
  onClick: () => void;
  inProgress?: boolean;
}

function SidebarItem({
  label,
  selected,
  onClick,
  inProgress,
}: SidebarItemProps) {
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={
          "relative flex w-full items-center px-4 py-2 text-left text-sm transition-colors " +
          (selected
            ? "rounded-md bg-white font-medium text-ink shadow-sm"
            : "rounded-md text-ink-muted hover:bg-white hover:text-ink")
        }
      >
        {selected && (
          <span
            aria-hidden
            className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-accent"
          />
        )}
        <span className="ml-2 flex-1">{label}</span>
        {inProgress && (
          <span
            aria-label="Captures in progress"
            title="Sayzo is working on something"
            className="ml-2 inline-flex h-2 w-2 shrink-0 animate-pulse rounded-full bg-blue-500"
          />
        )}
      </button>
    </li>
  );
}
