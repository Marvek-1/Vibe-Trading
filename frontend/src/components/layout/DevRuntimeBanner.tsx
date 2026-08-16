import { useState } from "react";
import { Code2, X } from "lucide-react";

const DISMISS_KEY = "qa-dev-banner-dismissed";

// Dev-only: makes the split runtime lanes visible so it's obvious which UI
// and which run root you're looking at. See CHANGELOG / conversation for why
// this exists: :5899 (vite-dev) vs :8899 (backend, possibly serving stale
// frontend/dist) and agent/runs (session) vs ~/.vibe-trading/runs (autopilot
// evidence) are easy to conflate.
export function DevRuntimeBanner() {
  const [dismissed, setDismissed] = useState(() => sessionStorage.getItem(DISMISS_KEY) === "1");

  if (!import.meta.env.DEV || dismissed) return null;

  return (
    <div className="flex items-center justify-between gap-3 border-b border-primary/30 bg-primary/10 px-4 py-1.5 text-xs text-primary">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="inline-flex items-center gap-1.5 font-medium">
          <Code2 className="h-3.5 w-3.5" />
          dev
        </span>
        <span>UI: vite-dev (:5899)</span>
        <span>API: proxied → 127.0.0.1:8899</span>
        <span>Runs: agent/runs (session)</span>
        <span>Evidence: ~/.vibe-trading/runs (autopilot)</span>
      </div>
      <button
        type="button"
        onClick={() => {
          sessionStorage.setItem(DISMISS_KEY, "1");
          setDismissed(true);
        }}
        className="shrink-0 rounded p-0.5 hover:bg-primary/20"
        aria-label="Dismiss"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
