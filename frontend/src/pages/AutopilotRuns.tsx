import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FlaskConical, Loader2, RefreshCw, ShieldAlert } from "lucide-react";
import { api, type AutopilotEvidenceRunItem } from "@/lib/api";
import { formatMetricVal } from "@/lib/formatters";

const MIN_STATISTICALLY_EVALUABLE_TRADES = 30;

export function AutopilotRuns() {
  const [runs, setRuns] = useState<AutopilotEvidenceRunItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(mode: "initial" | "refresh" = "refresh") {
    if (mode === "initial") setLoading(true);
    else setRefreshing(true);
    setError(null);
    try {
      const list = await api.listAutopilotEvidenceRuns(100);
      setRuns(Array.isArray(list) ? list : []);
    } catch (err) {
      setRuns([]);
      setError(err instanceof Error ? err.message : "Failed to load autopilot evidence runs.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void load("initial");
  }, []);

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <section className="flex flex-col gap-4 border-b border-border/40 pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3">
            <div className="glass-chip inline-flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <FlaskConical className="h-3.5 w-3.5" />
              Provenance, not proof
            </div>
            <div>
              <h1 className="text-3xl font-bold tracking-tight">Provenance-Verified Autopilot Runs</h1>
              <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
                Runs with a verified run card: implemented strategy body, receipted writes,
                complete artifacts, and honest hypothesis-test linkage. Stored under{" "}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">~/.vibe-trading/runs</code>.
                This confirms the run is <strong>real and honestly reported</strong> &mdash; it does
                not certify that the strategy is <strong>statistically valid</strong>. Check trade
                count and significance before trusting a Sharpe ratio.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void load("refresh")}
            disabled={refreshing}
            className="glass-btn"
          >
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Refresh
          </button>
        </section>

        <div className="text-sm text-muted-foreground">
          {runs.length} provenance-verified run{runs.length === 1 ? "" : "s"}
        </div>

        {loading ? (
          <div className="grid gap-3">
            {[1, 2, 3].map((item) => (
              <div key={item} className="glass-panel h-24 animate-pulse rounded-2xl" />
            ))}
          </div>
        ) : null}

        {!loading && error ? (
          <section className="glass-surface rounded-2xl border border-warning/30 p-5">
            <div className="flex items-center gap-2 font-medium text-warning">
              <AlertTriangle className="h-5 w-5" />
              Could not load autopilot evidence runs
            </div>
            <p className="mt-2 text-sm text-muted-foreground">{error}</p>
          </section>
        ) : null}

        {!loading && !error && runs.length === 0 ? (
          <section className="glass-panel rounded-2xl border border-dashed border-border/50 p-8 text-center">
            <FlaskConical className="mx-auto h-8 w-8 text-muted-foreground" />
            <h2 className="mt-3 font-medium">No provenance-verified autopilot runs yet</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Runs appear here once an autopilot hypothesis run's <code>run_card.json</code> has
              an implemented strategy, complete artifacts, and hypothesis-test intent.
            </p>
          </section>
        ) : null}

        {!loading && !error && runs.length > 0 ? (
          <section className="grid gap-3">
            {runs.map((run) => (
              <AutopilotRunRow key={run.run_dir} run={run} />
            ))}
          </section>
        ) : null}
      </div>
    </div>
  );
}

function AutopilotRunRow({ run }: { run: AutopilotEvidenceRunItem }) {
  const runName = run.run_dir.split(/[\\/]/).filter(Boolean).pop() || run.run_dir;
  const lowSample =
    run.trade_count != null && run.trade_count < MIN_STATISTICALLY_EVALUABLE_TRADES;
  return (
    <article className="glass-surface glass-card rounded-2xl p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 rounded bg-success/10 px-2 py-0.5 text-xs font-medium text-success">
              <CheckCircle2 className="h-3 w-3" />
              provenance verified
            </span>
            {lowSample ? (
              <span
                className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300"
                title={`Only ${run.trade_count} trade${run.trade_count === 1 ? "" : "s"} — metrics like Sharpe are not statistically meaningful at this sample size.`}
              >
                <ShieldAlert className="h-3 w-3" />
                insufficient sample &mdash; not statistically evaluable
              </span>
            ) : null}
            <span className="truncate font-mono text-sm font-medium">{runName}</span>
            {run.generated_at ? (
              <span className="text-xs text-muted-foreground">{formatRunDate(run.generated_at)}</span>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
            <span className="rounded border px-2 py-0.5">status: {run.strategy_implementation_status}</span>
            <span className="rounded border px-2 py-0.5">purpose: {run.run_purpose}</span>
          </div>
          <p className="break-all font-mono text-xs text-muted-foreground">{run.run_dir}</p>
          {run.signal_engine_sha256 ? (
            <p className="break-all font-mono text-xs text-muted-foreground">
              signal_engine.py sha256: {run.signal_engine_sha256}
            </p>
          ) : null}
        </div>

        <div className="grid grid-cols-3 gap-2 text-right sm:flex sm:flex-wrap sm:justify-end">
          <MetricPill
            label="Return"
            value={formatOptionalMetric("total_return", run.total_return)}
            muted={lowSample}
          />
          <MetricPill
            label="Sharpe"
            value={formatOptionalMetric("sharpe", run.sharpe)}
            muted={lowSample}
          />
          <MetricPill label="Trades" value={run.trade_count != null ? String(run.trade_count) : "-"} />
        </div>
      </div>
    </article>
  );
}

function MetricPill({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className="glass-panel rounded-lg px-3 py-1.5">
      <div className="text-[11px] uppercase text-muted-foreground">{label}</div>
      <div className={muted ? "font-mono text-sm font-medium text-muted-foreground line-through" : "font-mono text-sm font-medium"}>
        {value}
      </div>
    </div>
  );
}

function formatOptionalMetric(key: string, value: number | undefined): string {
  return Number.isFinite(value) ? formatMetricVal(key, value as number) : "-";
}

function formatRunDate(value: string): string {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}
