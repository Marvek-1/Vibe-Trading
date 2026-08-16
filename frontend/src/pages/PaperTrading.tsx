import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isFuturesClosedTrade, type PaperDecisionHealth, type PaperDecisionHealthWorker, type PaperProviderHealth, type PaperSessionSummary, type PositionMetadata } from "@/lib/api";
import { toast } from "sonner";

const SESSION_POLL_INTERVAL_MS = 5_000;
const NOTIFICATION_POLL_INTERVAL_MS = 5_000;
const HEARTBEAT_STALE_AFTER_MS = 20 * 60_000;
export const ALLOWED_LEVERAGE = [5, 10] as const;

function isAllowedLeverage(value: unknown): value is (typeof ALLOWED_LEVERAGE)[number] {
  return ALLOWED_LEVERAGE.includes(Number(value) as (typeof ALLOWED_LEVERAGE)[number]);
}

function isSupportedFuturesSession(s: PaperSessionSummary): boolean {
  const risk = s.session.risk_config;
  const usesFuturesAccounting = s.session.strategy_type === "futures_paper_engine"
    || risk?.portfolio_leverage === true
    || Number(risk?.fixed_margin_per_trade ?? 0) > 0;
  return usesFuturesAccounting && isAllowedLeverage(risk?.leverage);
}

// Grid Futures / Time Trading / Morning Glory: the same three groupings the
// original (now-retired) PaperTradingDashboard.tsx hardcoded to specific,
// long-dead session IDs. Classifying by strategy_type/session-id pattern
// instead means the grouping keeps working as sessions get created and
// retired, rather than needing a hardcoded ID list edited every time.
type PaperTab = "grid" | "timed" | "morning";
const TAB_LABELS: Record<PaperTab, string> = { grid: "Grid Futures", timed: "Time Trading", morning: "Morning Glory" };

function classifySessionTab(s: PaperSessionSummary): PaperTab {
  if (s.session.strategy_type === "funding_rate_zscore") return "morning";
  if (/grid|many_bots/i.test(s.session_id)) return "grid";
  return "timed";
}

function sessionDisplayName(s: PaperSessionSummary): string {
  const configured = s.database_account;
  if (configured) return `${configured.strategy_id} · ${configured.timeframe} · ${configured.leverage}x`;
  const arm = s.session_role === "control"
    ? "A · Control"
    : s.session_role === "candidate"
      ? "B · Candidate"
      : s.session_id;
  return s.regimen ? `${arm} · ${s.regimen}` : s.session_id;
}

// ─── Types ──────────────────────────────────────────────────────────
interface Position {
  symbol: string;
  perp: string;
  side: "LONG" | "SHORT";
  margin: string;
  leverage: string;
  notional: string;
  entryPrice: string;
  markPrice: string;
  liqPrice: string;
  tp: string;
  sl: string;
  unrealizedPnl: string;
  roi: string;
  duration: string;
}

interface ClosedTrade {
  time: string;
  symbol: string;
  side: "LONG" | "SHORT";
  margin: string;
  leverage: string;
  notional: string;
  entryPrice: string;
  exitPrice: string;
  exitReason: string;
  grossPnl: string;
  fees: string;
  funding: string;
  netPnl: string;
  roi: string;
  origin: "PAPER_BOOTSTRAP" | "STRATEGY";
}

interface MarginAlloc {
  symbol: string;
  color: string;
  pct: string;
  value: string;
  frac: number;
}

// ─── Formatting helpers ────────────────────────────────────────────
const cn = (...classes: (string | false | undefined)[]) => classes.filter(Boolean).join(" ");
const isPositive = (val: string) => val.trim().startsWith("+");

function usd(n: number): string {
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}
function usdSigned(n: number): string {
  return `${n >= 0 ? "+ " : "- "}${usd(Math.abs(n))}`;
}
function pctSigned(n: number): string {
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
}
function priceStr(n: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: n < 1 ? 6 : 2 });
}

type UnknownRecord = Record<string, unknown>;
export type MarketSource = "okx" | "binance" | "bybit" | "gate";
const MARKET_SOURCE_ROUTE: readonly MarketSource[] = ["okx", "binance", "bybit", "gate"];

interface MarketTelemetry {
  source: MarketSource | null;
  rawSource: string | null;
  observedAt: string | null;
  ageMs: number | null;
  status: "OK" | "STALE" | "MISSING" | "INVALID";
}

function asRecord(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null ? value as UnknownRecord : null;
}

function finiteNumber(value: unknown): number | null {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function firstString(records: Array<UnknownRecord | null>, keys: string[]): string | null {
  for (const record of records) {
    if (!record) continue;
    for (const key of keys) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return null;
}

export function normalizeMarketSource(value: string | null): MarketSource | null {
  if (!value) return null;
  const normalized = value.trim().toLowerCase();
  if (MARKET_SOURCE_ROUTE.includes(normalized as MarketSource)) return normalized as MarketSource;
  return null;
}

function timeframeToMs(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match = value.trim().toLowerCase().match(/^(\d+)\s*([mhd])$/);
  if (!match) return null;
  const amount = Number(match[1]);
  const unit = match[2];
  if (!Number.isFinite(amount) || amount <= 0) return null;
  const multiplier = unit === "m" ? 60_000 : unit === "h" ? 3_600_000 : 86_400_000;
  return amount * multiplier;
}

export function marketTelemetry(s: PaperSessionSummary, nowMs: number): MarketTelemetry {
  const root = asRecord(s);
  const latest = asRecord(s.latest_mark);
  const account = asRecord(s.database_account);
  const session = asRecord(s.session);
  const latestMarket = latest ? asRecord(latest.market_data) : null;
  const accountMarket = account ? asRecord(account.market_data) : null;

  const records = [latestMarket, latest, accountMarket, account, session, root];
  const rawSource = firstString(records, ["market_data_source", "price_source", "provider"]);
  const source = normalizeMarketSource(rawSource);
  const observedAt = firstString(records, [
    "market_data_observed_at",
    "price_observed_at",
    "observed_at",
    "mark_timestamp",
    "timestamp",
  ]);

  const observedMs = observedAt ? Date.parse(observedAt) : Number.NaN;
  const ageMs = Number.isFinite(observedMs) ? Math.max(0, nowMs - observedMs) : null;
  const configuredTimeframe = account?.timeframe ?? session?.timeframe;
  const timeframeMs = timeframeToMs(configuredTimeframe);
  const staleAfterMs = Math.max(
    SESSION_POLL_INTERVAL_MS * 3,
    timeframeMs != null ? timeframeMs * 2 + 60_000 : 20_000,
  );

  let status: MarketTelemetry["status"];
  if (rawSource && !source) status = "INVALID";
  else if (!source || ageMs === null) status = "MISSING";
  else if (ageMs > staleAfterMs) status = "STALE";
  else status = "OK";

  return { source, rawSource, observedAt, ageMs, status };
}

function moneyOrDash(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : usd(value);
}

function signedMoneyOrDash(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : usdSigned(value);
}

function signedPctOrDash(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : pctSigned(value);
}

function formatAge(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "age unavailable";
  if (ms < 1_000) return "just now";
  const seconds = Math.floor(ms / 1_000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m ago`;
}

function valueColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "text-gray-500";
  return value >= 0 ? "text-emerald-400" : "text-red-400";
}
function fmtDuration(ms: number): string {
  if (ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}:${sec.toString().padStart(2, "0")}`;
}

const DONUT_COLORS = ["#f97316", "#3b82f6", "#22c55e", "#a855f7", "#ef4444", "#06b6d4", "#eab308", "#ec4899"];

const MarketSourceBadge: React.FC<{
  source: MarketSource | null;
  rawSource?: string | null;
  status: MarketTelemetry["status"];
}> = ({ source, rawSource, status }) => {
  const label = source?.toUpperCase() ?? (rawSource ? `INVALID (${rawSource})` : "UNAVAILABLE");
  return (
    <span className={cn(
      "inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-semibold tracking-wide",
      status === "OK"
        ? "border-emerald-800 bg-emerald-950/40 text-emerald-400"
        : status === "STALE"
          ? "border-amber-800 bg-amber-950/40 text-amber-400"
          : "border-red-900 bg-red-950/30 text-red-400",
    )}>
      {label}
    </span>
  );
};

const MarketRoute: React.FC<{ active: MarketSource | null }> = ({ active }) => (
  <div className="flex flex-wrap items-center gap-1.5 text-xs text-gray-500" aria-label="Market data provider priority">
    <span>Feed priority</span>
    {MARKET_SOURCE_ROUTE.map((source, index) => (
      <span key={source} className="inline-flex items-center gap-1.5">
        {index > 0 && <span aria-hidden="true" className="text-gray-700">→</span>}
        <span className={cn(
          "rounded border px-1.5 py-0.5 uppercase tracking-wide",
          active === source
            ? "border-emerald-800 bg-emerald-950/40 text-emerald-400"
            : "border-gray-800 bg-gray-900/50 text-gray-500",
        )}>
          {source}
        </span>
      </span>
    ))}
    <span className="text-gray-600">latest completed cycle</span>
  </div>
);

const OperationsSummary: React.FC<{
  sessions: PaperSessionSummary[] | null;
  providerHealth: PaperProviderHealth | null;
  providerHealthError: string | null;
  decisionHealth: PaperDecisionHealth | null;
  refreshAgeMs: number | null;
  nowMs: number;
}> = ({ sessions, providerHealth, providerHealthError, decisionHealth, refreshAgeMs, nowMs }) => {
  const workers = sessions?.filter((session) => session.database_account) ?? [];
  const freshWorkers = workers.filter((session) => {
    const heartbeat = session.database_account?.last_heartbeat;
    const timestamp = heartbeat ? Date.parse(heartbeat) : Number.NaN;
    return Number.isFinite(timestamp) && nowMs - timestamp <= HEARTBEAT_STALE_AFTER_MS;
  }).length;
  const healthyProviders = providerHealth?.providers.filter((provider) => provider.status === "ok").length ?? 0;
  const decisionWorkers = decisionHealth?.workers ?? [];
  const evaluated = decisionWorkers.reduce((total, worker) => total + worker.window.signals_evaluated, 0);
  const fills = decisionWorkers.reduce((total, worker) => total + worker.window.paper_orders_filled, 0);
  const apiState = refreshAgeMs !== null && refreshAgeMs <= SESSION_POLL_INTERVAL_MS * 3 ? "Connected" : "Refresh delayed";

  return (
    <section className="mb-5 border border-gray-800 bg-gray-950/45" aria-label="Paper operations summary">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-800 px-4 py-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-gray-500">Operations</p>
          <p className="mt-1 text-sm text-gray-200">Paper execution only. Live capital execution remains disabled.</p>
        </div>
        <span className={cn(
          "rounded border px-2 py-1 text-xs font-semibold",
          apiState === "Connected" ? "border-emerald-800 bg-emerald-950/40 text-emerald-300" : "border-amber-800 bg-amber-950/40 text-amber-300",
        )}>{apiState}</span>
      </div>
      <dl className="grid divide-y divide-gray-800 sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-5">
        <div className="px-4 py-3"><dt className="text-xs text-gray-500">Execution</dt><dd className="mt-1 text-sm font-semibold text-sky-300">PAPER</dd></div>
        <div className="px-4 py-3"><dt className="text-xs text-gray-500">Workers fresh</dt><dd className="mt-1 text-sm font-semibold text-gray-100">{sessions === null ? "Checking…" : `${freshWorkers}/${workers.length}`}</dd></div>
        <div className="px-4 py-3"><dt className="text-xs text-gray-500">Market providers</dt><dd className="mt-1 text-sm font-semibold text-gray-100">{providerHealthError ? "Unavailable" : providerHealth ? `${healthyProviders}/${providerHealth.providers.length} reachable` : "Checking…"}</dd></div>
        <div className="px-4 py-3"><dt className="text-xs text-gray-500">Signals evaluated, 24h</dt><dd className="mt-1 text-sm font-semibold text-gray-100">{decisionHealth ? evaluated.toLocaleString() : "Checking…"}</dd></div>
        <div className="px-4 py-3"><dt className="text-xs text-gray-500">Paper fills, 24h</dt><dd className="mt-1 text-sm font-semibold text-gray-100">{decisionHealth ? fills.toLocaleString() : "Checking…"}</dd></div>
      </dl>
    </section>
  );
};

const ProviderHealthBar: React.FC<{
  health: PaperProviderHealth | null;
  error: string | null;
  active: MarketSource | null;
  nowMs: number;
}> = ({ health, error, active, nowMs }) => {
  const checkedMs = health ? Date.parse(health.checked_at) : Number.NaN;
  const ageMs = Number.isFinite(checkedMs) ? Math.max(0, nowMs - checkedMs) : null;
  return (
    <section className="mb-5 border-y border-gray-800 bg-gray-900/35 px-3 py-2.5" aria-label="Market provider health">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <span className="font-semibold uppercase tracking-wider text-gray-400">Provider health</span>
        {(health?.providers ?? []).map((provider) => {
          const isHealthy = provider.status === "ok";
          const isActive = provider.provider === active;
          return (
            <span key={provider.provider} className="inline-flex items-center gap-1.5">
              <span className={cn("h-2 w-2 rounded-full", isHealthy ? "bg-emerald-500" : "bg-red-500")} aria-hidden="true" />
              <span className={cn("font-semibold uppercase", isActive ? "text-emerald-400" : "text-gray-300")}>
                {provider.provider}
              </span>
              <span className={isHealthy ? "text-gray-500" : "text-red-400"}>
                {isHealthy ? `${provider.latency_ms}ms` : provider.error ?? "unavailable"}
              </span>
              {isActive && <span className="text-emerald-500">selected</span>}
            </span>
          );
        })}
        {!health && !error && <span className="text-gray-500">Checking all providers…</span>}
        {error && <span className="text-red-400">Probe unavailable: {error}</span>}
        <span className="ml-auto text-gray-600">Checked {formatAge(ageMs)}</span>
      </div>
    </section>
  );
};

// ─── Components ─────────────────────────────────────────────────────

const StatCard: React.FC<{
  label: string;
  value: string;
  sub: string;
  valueColor?: string;
  glow?: boolean;
}> = ({ label, value, sub, valueColor = "text-white", glow }) => (
  <div className={cn(
    "relative rounded-xl border border-gray-800 bg-gray-900/80 p-4",
    glow && "before:absolute before:inset-0 before:rounded-xl before:bg-emerald-500/5",
  )}>
    <div className="relative">
      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={cn("mt-1 text-xl font-bold", valueColor)}>{value}</p>
      <p className="mt-0.5 text-xs text-gray-500">{sub}</p>
    </div>
  </div>
);

const SideBadge: React.FC<{ side: "LONG" | "SHORT" }> = ({ side }) => {
  const isLong = side === "LONG";
  return (
    <span className={cn(
      "inline-flex items-center rounded px-2.5 py-0.5 text-xs font-bold",
      isLong ? "bg-emerald-500/10 text-emerald-400" : "bg-red-500/10 text-red-400",
    )}>
      {side}
    </span>
  );
};

const DonutChart: React.FC<{ data: MarginAlloc[]; total: string }> = ({ data, total }) => {
  const radius = 70;
  const stroke = 22;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  if (data.length === 0) {
    return <p className="text-sm text-gray-500 py-10 text-center">No leveraged positions are currently open.</p>;
  }

  return (
    <div className="flex items-center gap-6">
      <div className="relative" style={{ width: radius * 2 + stroke, height: radius * 2 + stroke }}>
        <svg width={radius * 2 + stroke} height={radius * 2 + stroke} className="-rotate-90">
          {data.map((item, i) => {
            const dash = item.frac * circumference;
            const seg = (
              <circle
                key={i}
                cx={radius + stroke / 2}
                cy={radius + stroke / 2}
                r={radius}
                fill="none"
                stroke={item.color}
                strokeWidth={stroke}
                strokeDasharray={`${dash} ${circumference - dash}`}
                strokeDashoffset={-offset}
                strokeLinecap="butt"
              />
            );
            offset += dash;
            return seg;
          })}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-lg font-bold text-white">{total}</span>
          <span className="text-xs text-gray-500">Total Margin</span>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        {data.map((item) => (
          <div key={item.symbol} className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: item.color }} />
            <span className="text-xs text-gray-400">{item.symbol}</span>
            <span className="text-xs text-gray-500">{item.pct}</span>
            <span className="ml-auto text-xs text-gray-500">{item.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

const EquityCurve: React.FC<{
  points: number[];
  labels: string[];
  currentEquity: number | null;
  changePct: number | null;
}> = ({ points, labels, currentEquity, changePct }) => {
  const w = 360;
  const h = 140;
  const hasEquity = currentEquity != null && Number.isFinite(currentEquity);
  const hasChange = changePct != null && Number.isFinite(changePct);
  const positive = hasChange ? changePct >= 0 : true;
  const stroke = hasChange ? (positive ? "#22c55e" : "#ef4444") : "#6b7280";

  if (points.length < 2) {
    return (
      <div>
        <p className={cn("text-2xl font-bold", hasEquity ? valueColor(currentEquity) : "text-gray-500")}>
          {moneyOrDash(currentEquity)}
        </p>
        <p className="text-xs text-gray-500">Current Equity</p>
        <p className="text-sm text-gray-600 mt-8 text-center">Not enough live marks yet.</p>
      </div>
    );
  }

  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const step = w / (points.length - 1);
  const path = points.map((p, i) => {
    const x = i * step;
    const y = h - ((p - min) / range) * h;
    return `${i === 0 ? "M" : "L"} ${x} ${y}`;
  }).join(" ");
  const areaPath = `${path} L ${w} ${h} L 0 ${h} Z`;

  return (
    <div>
      <div className="flex items-baseline gap-6">
        <div>
          <p className={cn("text-2xl font-bold", hasEquity ? valueColor(currentEquity) : "text-gray-500")}>
            {moneyOrDash(currentEquity)}
          </p>
          <p className="text-xs text-gray-500">Current Equity</p>
        </div>
        <div>
          <p className={cn("text-lg font-bold", hasChange ? valueColor(changePct) : "text-gray-500")}>
            {signedPctOrDash(changePct)}
          </p>
          <p className="text-xs text-gray-500">Session Change</p>
        </div>
      </div>
      <div className="mt-4">
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
          <defs>
            <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity="0.25" />
              <stop offset="100%" stopColor={stroke} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={areaPath} fill="url(#eqGrad)" />
          <path d={path} fill="none" stroke={stroke} strokeWidth="2" />
        </svg>
        <div className="mt-1 flex justify-between text-xs text-gray-600">
          {labels.map((label, i) => <span key={`${label}-${i}`}>{label}</span>)}
        </div>
      </div>
    </div>
  );
};

const HealthRing: React.FC<{ marginUsagePct: number | null; leveraged: boolean }> = ({ marginUsagePct, leveraged }) => {
  const radius = 65;
  const stroke = 12;
  const circumference = 2 * Math.PI * radius;
  const hasUsage = marginUsagePct != null && Number.isFinite(marginUsagePct);
  const usage = hasUsage ? Math.max(0, Math.min(1, marginUsagePct)) : null;
  const health = !leveraged ? 1 : usage === null ? null : 1 - usage;
  const dash = (health ?? 0) * circumference;
  const risk = !leveraged
    ? "None"
    : usage === null
      ? "Unknown"
      : usage >= 0.8
        ? "High"
        : usage >= 0.5
          ? "Elevated"
          : "Low";
  const ringColor = !leveraged
    ? "#22c55e"
    : usage === null
      ? "#6b7280"
      : usage >= 0.8
        ? "#ef4444"
        : usage >= 0.5
          ? "#f59e0b"
          : "#22c55e";

  return (
    <div className="flex flex-col items-center">
      <div className="relative" style={{ width: radius * 2 + stroke, height: radius * 2 + stroke }}>
        <svg width={radius * 2 + stroke} height={radius * 2 + stroke} className="-rotate-90">
          <circle cx={radius + stroke / 2} cy={radius + stroke / 2} r={radius} fill="none" stroke="#1f2937" strokeWidth={stroke} />
          <circle
            cx={radius + stroke / 2}
            cy={radius + stroke / 2}
            r={radius}
            fill="none"
            stroke={ringColor}
            strokeWidth={stroke}
            strokeDasharray={`${dash} ${circumference - dash}`}
            strokeLinecap="round"
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-3xl font-bold text-white">{health === null ? "—" : `${Math.round(health * 100)}%`}</span>
          <span className="text-sm" style={{ color: ringColor }}>
            {!leveraged ? "Unleveraged" : health === null ? "No live margin data" : health >= 0.5 ? "Healthy" : "At Risk"}
          </span>
        </div>
      </div>
      <div className="mt-4 flex w-full justify-between">
        <div>
          <p className="text-xs text-gray-500">Margin Usage</p>
          <p className="text-lg font-bold" style={{ color: ringColor }}>
            {!leveraged ? "N/A" : usage === null ? "—" : `${(usage * 100).toFixed(2)}%`}
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-gray-500">Risk Level</p>
          <p className="text-lg font-bold" style={{ color: ringColor }}>{risk}</p>
        </div>
      </div>
    </div>
  );
};

// ─── Data adapters: real PaperSessionSummary -> this dashboard's view models ─

function buildPositions(s: PaperSessionSummary, nowMs: number): Position[] {
  const latest = s.latest_mark;
  const positionMeta = s.book?.position_metadata ?? {};
  // Futures-engine marks (control_*/candidate_* sessions) carry per-position
  // detail in open_positions[], not the spot mark's prices/position_values/
  // position_pnl maps -- reading only those left mark/notional/uPnL/ROI blank
  // for every futures position row even though the engine had the numbers.
  const futuresMarkBySymbol = new Map((latest?.open_positions ?? []).map((p) => [p.symbol, p]));
  const symbols = Array.from(new Set([
    ...s.session.symbols,
    ...Object.keys(s.book?.positions ?? {}),
  ]));
  return symbols
    .map((sym): Position | null => {
      const qty = s.book?.positions?.[sym] ?? 0;
      if (Math.abs(qty) < 1e-9) return null;
      const meta: PositionMetadata | undefined = positionMeta[sym];
      const futuresMark = futuresMarkBySymbol.get(sym);
      const leveraged = !!meta && meta.leverage > 1;
      const currentPrice = futuresMark?.mark_price ?? latest?.prices?.[sym];
      const value = futuresMark?.notional ?? latest?.position_values?.[sym];
      const symPnl = futuresMark?.unrealized_net_pnl ?? latest?.position_pnl?.[sym];
      const entryPrice = futuresMark?.entry_price ?? s.session.entry_prices?.[sym] ?? meta?.entry_price;
      const direction = meta?.direction ?? (qty >= 0 ? 1 : -1);
      const margin = futuresMark?.isolated_margin ?? meta?.margin;
      const roi = futuresMark ? futuresMark.margin_roi_pct / 100
        : leveraged && margin && margin > 0 && symPnl != null ? symPnl / margin : null;
      const durationMs = (futuresMark?.entry_time ?? meta?.entry_time) ? nowMs - new Date((futuresMark?.entry_time ?? meta!.entry_time)!).getTime() : null;
      const liqPrice = futuresMark?.liquidation_price ?? (leveraged && meta ? meta.liquidation_price : null);
      const tpPrice = futuresMark?.take_profit_price ?? meta?.take_profit_price;
      const slPrice = futuresMark?.stop_loss_price ?? meta?.stop_loss_price;
      return {
        symbol: sym,
        perp: leveraged || futuresMark ? "Perp" : "Spot",
        side: direction >= 0 ? "LONG" : "SHORT",
        margin: margin != null ? usd(margin) : "—",
        leverage: futuresMark ? `${futuresMark.leverage}x` : meta ? `${meta.leverage}x` : "1x",
        notional: value != null ? usd(Math.abs(value)) : "—",
        entryPrice: entryPrice != null ? priceStr(entryPrice) : "—",
        markPrice: currentPrice != null ? priceStr(currentPrice) : "—",
        liqPrice: liqPrice != null && liqPrice > 0 ? priceStr(liqPrice) : "—",
        tp: tpPrice != null ? `TP: ${priceStr(tpPrice)}` : "",
        sl: slPrice != null ? `SL: ${priceStr(slPrice)}` : "",
        unrealizedPnl: symPnl != null ? usdSigned(symPnl) : "—",
        roi: roi != null ? pctSigned(roi) : "—",
        duration: durationMs != null ? fmtDuration(durationMs) : "—",
      };
    })
    .filter((p): p is Position => p !== null);
}

function buildClosedTrades(s: PaperSessionSummary): ClosedTrade[] {
  const positionMeta = s.book?.position_metadata ?? {};
  return [...s.recent_trades]
    .reverse()
    .filter((tr) => isFuturesClosedTrade(tr) ? tr.net_pnl != null : tr.realized_pnl != null)
    .map((tr): ClosedTrade => {
      if (isFuturesClosedTrade(tr)) {
        // FuturesPaperEngine's ClosedTrade rows carry their own leverage/margin/notional
        // per trade -- unlike the spot log, no lookup into current open-position
        // metadata is needed (or correct, since the position closed).
        return {
          time: new Date(tr.exit_time).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
          symbol: tr.symbol,
          side: tr.side === "long" ? "LONG" : "SHORT",
          margin: usd(tr.margin_used),
          leverage: `${tr.leverage}x`,
          notional: usd(tr.notional),
          entryPrice: priceStr(tr.entry_price),
          exitPrice: priceStr(tr.exit_price),
          exitReason: tr.exit_reason,
          grossPnl: usdSigned(tr.gross_pnl),
          fees: `- ${usd(tr.entry_fee + tr.exit_fee)}`,
          funding: tr.funding_paid ? usdSigned(-tr.funding_paid) : "—",
          netPnl: usdSigned(tr.net_pnl),
          roi: pctSigned(tr.roi_pct / 100),
          origin: /bootstrap/i.test(tr.entry_reason) || /bootstrap/i.test(tr.exit_reason) ? "PAPER_BOOTSTRAP" : "STRATEGY",
        };
      }
      const meta = positionMeta[tr.symbol];
      return {
        time: new Date(tr.timestamp).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        symbol: tr.symbol,
        side: tr.side === "BUY" ? "SHORT" : "LONG", // a SELL that closes a long-only equal-weight book was the LONG being reduced
        margin: meta?.margin != null ? usd(meta.margin) : "—",
        leverage: meta ? `${meta.leverage}x` : "1x",
        notional: usd(tr.notional),
        entryPrice: tr.entry_price != null ? priceStr(tr.entry_price) : "—",
        exitPrice: priceStr(tr.price),
        exitReason: tr.reason,
        grossPnl: tr.gross_pnl != null ? usdSigned(tr.gross_pnl) : "—",
        fees: tr.total_fees != null || tr.fee_paid != null ? `- ${usd(tr.total_fees ?? tr.fee_paid!)}` : "—",
        funding: "—", // not modeled by paper_session.py
        netPnl: tr.net_pnl != null ? usdSigned(tr.net_pnl) : "—",
        roi: meta?.margin && tr.net_pnl != null && meta.margin > 0 ? pctSigned(tr.net_pnl / meta.margin) : "—",
        origin: "STRATEGY",
      };
    });
}

function buildMarginAlloc(s: PaperSessionSummary): { slices: MarginAlloc[]; totalMargin: number } {
  const positionMeta = s.book?.position_metadata ?? {};
  const openPositions = s.book?.positions ?? {};
  const leveraged = Object.entries(positionMeta).filter(
    ([symbol, metadata]) => Math.abs(openPositions[symbol] ?? 0) >= 1e-9 && metadata.leverage > 1 && metadata.margin > 0,
  );
  const totalMargin = leveraged.reduce((sum, [, m]) => sum + m.margin, 0);
  const slices: MarginAlloc[] = leveraged.map(([sym, m], i) => ({
    symbol: sym,
    color: DONUT_COLORS[i % DONUT_COLORS.length],
    pct: totalMargin ? `${((m.margin / totalMargin) * 100).toFixed(1)}%` : "0%",
    value: usd(m.margin),
    frac: totalMargin ? m.margin / totalMargin : 0,
  }));
  return { slices, totalMargin };
}

// ─── Worker status overview (all six control/candidate workers at a glance) ─
const WORKER_ORDER = ["control_5m", "control_10m", "control_15m", "candidate_5m", "candidate_10m", "candidate_15m"] as const;

function workerRank(id: string): number {
  const idx = WORKER_ORDER.indexOf(id as (typeof WORKER_ORDER)[number]);
  return idx === -1 ? WORKER_ORDER.length : idx;
}

const WorkerCard: React.FC<{
  s: PaperSessionSummary;
  nowMs: number;
  decision: PaperDecisionHealthWorker | undefined;
  selected: boolean;
  onSelect: () => void;
}> = ({ s, nowMs, decision, selected, onSelect }) => {
  const account = s.database_account;
  const positions = buildPositions(s, nowMs);
  const openCount = positions.length;
  const unrealizedPnlValues = positions
    .map((position) => finiteNumber(position.unrealizedPnl.replace(/[^0-9.-]/g, "")))
    .filter((value): value is number => value !== null);
  const aggregateUnrealizedPnl = unrealizedPnlValues.length
    ? unrealizedPnlValues.reduce((sum, value) => sum + value, 0)
    : null;
  const realizedPnl = account?.realized_pnl ?? s.trade_stats?.overall?.realized_pnl ?? null;
  const heartbeatMs = account?.last_heartbeat ? Date.parse(account.last_heartbeat) : Number.NaN;
  const heartbeatAgeMs = Number.isFinite(heartbeatMs) ? Math.max(0, nowMs - heartbeatMs) : null;
  const heartbeatState = heartbeatAgeMs === null
    ? "unknown"
    : heartbeatAgeMs > HEARTBEAT_STALE_AFTER_MS
      ? "stale"
      : s.status === "running"
        ? "fresh"
        : "stopped";
  const market = marketTelemetry(s, nowMs);
  const evaluatedSignals = finiteNumber(decision?.latest_funnel.signals_evaluated) ?? 0;
  const trueSignals = finiteNumber(decision?.latest_funnel.signals_true) ?? 0;

  return (
    <button
      onClick={onSelect}
      className={cn(
        "flex flex-col gap-2 rounded-xl border bg-gray-900/80 p-4 text-left transition-colors",
        selected ? "border-emerald-500" : "border-gray-800 hover:border-gray-700",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-bold text-white">{account?.worker_id ?? s.session_id}</span>
        <span className={cn(
          "inline-block h-2 w-2 rounded-full",
          heartbeatState === "fresh" ? "bg-emerald-500" : heartbeatState === "stale" ? "bg-amber-500" : "bg-gray-600",
        )} />
      </div>

      <div className="flex flex-wrap items-center gap-1.5 text-xs text-gray-500">
        {account && <span className="rounded bg-gray-800 px-1.5 py-0.5 text-gray-300">{account.strategy_id}</span>}
        {account && <span className="rounded bg-blue-900/40 px-1.5 py-0.5 text-blue-400 border border-blue-800">{account.leverage}x</span>}
        {account && <span>{account.timeframe}</span>}
      </div>

      <div className="text-sm">
        {openCount === 0 ? (
          <span className="text-gray-600">No open positions · {s.trade_count} closed retained</span>
        ) : (
          // A glance card summarizes; per-symbol rows belong in the Open
          // Positions table on the full worker view (click through for that).
          <div className="flex items-center justify-between">
            <span className="text-gray-300">{openCount} open position{openCount === 1 ? "" : "s"}</span>
            <span className={cn("font-semibold", valueColor(aggregateUnrealizedPnl))}>
              {signedMoneyOrDash(aggregateUnrealizedPnl)}
            </span>
          </div>
        )}
      </div>

      <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
        <span>Closed: {s.trade_count}</span>
        <span className={valueColor(realizedPnl)}>{signedMoneyOrDash(realizedPnl)}</span>
      </div>

      <div className="flex items-center justify-between text-xs">
        <span className={account?.ledger_status === "OK" ? "text-emerald-400" : "text-red-400"}>
          Ledger: {account?.ledger_status ?? "UNAVAILABLE"}
        </span>
        <span className={heartbeatState === "stale" ? "text-amber-400" : "text-gray-500"}>
          Heartbeat: {formatAge(heartbeatAgeMs)}
        </span>
      </div>

      <div className="flex items-center justify-between text-xs text-gray-500">
        <span className="inline-flex items-center gap-1.5">
          Feed <MarketSourceBadge source={market.source} rawSource={market.rawSource} status={market.status} />
        </span>
        <span className={market.status === "OK" ? "text-emerald-400" : market.status === "STALE" ? "text-amber-400" : "text-red-400"}>
          {market.status}
        </span>
      </div>

      {decision && (
        <div className="border-t border-gray-800 pt-2 text-xs text-gray-500">
          <span className="text-gray-400">Decision: </span>
          {evaluatedSignals} evaluated · {trueSignals} signals
          {decision.latest_rejections.strategy ? ` · ${decision.latest_rejections.strategy}` : ""}
        </div>
      )}
    </button>
  );
};

const WorkerStatusGrid: React.FC<{
  sessions: PaperSessionSummary[];
  nowMs: number;
  decisionByWorker: Map<string, PaperDecisionHealthWorker>;
  selectedSessionId: string | null;
  onSelect: (s: PaperSessionSummary) => void;
}> = ({ sessions, nowMs, decisionByWorker, selectedSessionId, onSelect }) => {
  const workers = useMemo(
    () => sessions
      .filter((s) => s.database_account)
      .sort((a, b) => workerRank(a.database_account!.worker_id) - workerRank(b.database_account!.worker_id)),
    [sessions],
  );
  if (workers.length === 0) return null;
  return (
    <div className="mb-5">
      <h2 className="mb-3 text-lg font-bold text-white">Worker Status <span className="text-gray-500">({workers.length})</span></h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
        {workers.map((w) => (
          <WorkerCard key={w.session_id} s={w} nowMs={nowMs} decision={w.database_account ? decisionByWorker.get(w.database_account.worker_id) : undefined} selected={w.session_id === selectedSessionId} onSelect={() => onSelect(w)} />
        ))}
      </div>
    </div>
  );
};

// ─── Main Page ──────────────────────────────────────────────────────
export function PaperTrading() {
  const notificationCursor = useRef<string | undefined>(undefined);
  useEffect(() => {
    const poll = async () => {
      try {
        const events = await api.getPaperTradingNotifications(notificationCursor.current);
        for (const event of events) {
          toast(event.title, { description: event.message });
          notificationCursor.current = event.created_at;
        }
      } catch { /* dashboard polling must not affect trading visibility */ }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), NOTIFICATION_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);
  const [sessions, setSessions] = useState<PaperSessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastSuccessfulRefreshMs, setLastSuccessfulRefreshMs] = useState<number | null>(null);
  const [providerHealth, setProviderHealth] = useState<PaperProviderHealth | null>(null);
  const [providerHealthError, setProviderHealthError] = useState<string | null>(null);
  const [decisionHealth, setDecisionHealth] = useState<PaperDecisionHealth | null>(null);
  const [activeTab, setActiveTab] = useState<PaperTab>("timed");
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const mountedRef = useRef(false);
  const providerMountedRef = useRef(false);
  const userPickedTabRef = useRef(false);
  const requestSequenceRef = useRef(0);
  const appliedSequenceRef = useRef(0);

  useEffect(() => {
    const clock = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(clock);
  }, []);

  useEffect(() => {
    let active = true;
    const loadDecisionHealth = async () => {
      try {
        const health = await api.getPaperDecisionHealth();
        if (active) setDecisionHealth(health);
      } catch {
        if (active) setDecisionHealth(null);
      }
    };
    void loadDecisionHealth();
    const timer = window.setInterval(() => void loadDecisionHealth(), SESSION_POLL_INTERVAL_MS);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const decisionByWorker = useMemo(
    () => new Map((decisionHealth?.workers ?? []).map((worker) => [worker.worker_id, worker])),
    [decisionHealth],
  );

  const load = useCallback(async () => {
    const requestSequence = ++requestSequenceRef.current;
    try {
      const next = await api.listPaperSessions("active");
      if (!mountedRef.current || requestSequence < appliedSequenceRef.current) return;

      appliedSequenceRef.current = requestSequence;
      setSessions(next.filter(isSupportedFuturesSession));
      setLastSuccessfulRefreshMs(Date.now());
      setError(null);
    } catch (err) {
      if (!mountedRef.current || requestSequence < appliedSequenceRef.current) return;
      setError(err instanceof Error ? err.message : "Failed to load live paper sessions");
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    setSessions(null);
    setSelectedSessionId(null);
    load();
    const timer = window.setInterval(load, SESSION_POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    providerMountedRef.current = true;
    const loadProviderHealth = async () => {
      try {
        const health = await api.getPaperProviderHealth();
        if (!providerMountedRef.current) return;
        setProviderHealth(health);
        setProviderHealthError(null);
      } catch (err) {
        if (!providerMountedRef.current) return;
        setProviderHealthError(err instanceof Error ? err.message : "provider health unavailable");
      }
    };
    void loadProviderHealth();
    const timer = window.setInterval(() => void loadProviderHealth(), 15_000);
    return () => {
      providerMountedRef.current = false;
      window.clearInterval(timer);
    };
  }, []);

  const tabCounts = useMemo(() => {
    const counts: Record<PaperTab, number> = { grid: 0, timed: 0, morning: 0 };
    for (const s of sessions ?? []) counts[classifySessionTab(s)]++;
    return counts;
  }, [sessions]);

  useEffect(() => {
    if (!sessions || userPickedTabRef.current) return;
    if (tabCounts[activeTab] > 0) return;
    const firstNonEmpty = (Object.keys(TAB_LABELS) as PaperTab[]).find((t) => tabCounts[t] > 0);
    if (firstNonEmpty) setActiveTab(firstNonEmpty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions, tabCounts]);

  const visibleSessions = useMemo(
    () => (sessions ?? []).filter((s) => classifySessionTab(s) === activeTab),
    [sessions, activeTab],
  );
  const s = useMemo(
    () => visibleSessions.find((x) => x.session_id === selectedSessionId) ?? visibleSessions[0] ?? null,
    [visibleSessions, selectedSessionId],
  );

  if (error && !sessions) {
    return <div className="min-h-screen bg-[#0a0e17] p-5 text-red-400">{error}</div>;
  }
  if (!s) {
    return (
      <div className="min-h-screen bg-[#0a0e17] p-5 text-gray-100">
        <h1 className="text-2xl font-bold text-white mb-4">Live Paper Trading Dashboard</h1>
        {error && (
          <div className="mb-4 rounded-lg border border-amber-800 bg-amber-950/40 p-3 text-sm text-amber-300">
            Live refresh failed: {error}. No zero values were substituted.
          </div>
        )}
        <WorkerStatusGrid
          sessions={sessions ?? []}
          nowMs={nowMs}
          decisionByWorker={decisionByWorker}
          selectedSessionId={selectedSessionId}
          onSelect={(w) => { userPickedTabRef.current = true; setActiveTab(classifySessionTab(w)); setSelectedSessionId(w.session_id); }}
        />
        <OperationsSummary
          sessions={sessions}
          providerHealth={providerHealth}
          providerHealthError={providerHealthError}
          decisionHealth={decisionHealth}
          refreshAgeMs={lastSuccessfulRefreshMs === null ? null : nowMs - lastSuccessfulRefreshMs}
          nowMs={nowMs}
        />
        <ProviderHealthBar health={providerHealth} error={providerHealthError} active={null} nowMs={nowMs} />
        <div className="flex gap-2 mb-4">
          {(Object.keys(TAB_LABELS) as PaperTab[]).map((tab) => (
            <button
              key={tab}
              onClick={() => { userPickedTabRef.current = true; setActiveTab(tab); setSelectedSessionId(null); }}
              className={cn(
                "px-3 py-1.5 rounded-lg text-sm font-medium border",
                tab === activeTab ? "border-blue-500 bg-blue-500/10 text-blue-400" : "border-gray-800 text-gray-500",
              )}
            >
              {TAB_LABELS[tab]} ({tabCounts[tab]})
            </button>
          ))}
        </div>
        <p className="text-gray-500">
          {sessions === null ? "Loading…" : `No active ${TAB_LABELS[activeTab]} sessions right now.`}
        </p>
      </div>
    );
  }

  const latest = s.latest_mark;
  const account = s.database_account;
  const overall = s.trade_stats?.overall;
  const positions = buildPositions(s, nowMs);
  const closedTrades = buildClosedTrades(s);
  const { slices: marginAlloc, totalMargin } = buildMarginAlloc(s);
  const market = marketTelemetry(s, nowMs);
  const selectedHeartbeatMs = account?.last_heartbeat ? Date.parse(account.last_heartbeat) : Number.NaN;
  const selectedHeartbeatAgeMs = Number.isFinite(selectedHeartbeatMs) ? Math.max(0, nowMs - selectedHeartbeatMs) : null;
  const selectedHeartbeatFresh = selectedHeartbeatAgeMs !== null && selectedHeartbeatAgeMs <= HEARTBEAT_STALE_AFTER_MS;

  const initialCapital = finiteNumber(account?.initial_capital ?? s.session.initial_cash);
  const equity = finiteNumber(account?.current_equity ?? latest?.equity);
  const pnl = finiteNumber(latest?.pnl)
    ?? (equity != null && initialCapital != null ? equity - initialCapital : null);
  // FuturesPaperEngine persists pnl_pct as a percentage (for example 0.053),
  // while pctSigned expects a fractional value (0.00053). Normalize only the
  // persisted mark; the fallback is already a fraction.
  const persistedPnlPct = finiteNumber(latest?.pnl_pct);
  const pnlPct = persistedPnlPct != null
    ? persistedPnlPct / 100
    : (pnl != null && initialCapital != null && initialCapital !== 0 ? pnl / initialCapital : null);

  const accountCash = finiteNumber(account?.cash_available);
  const accountMargin = finiteNumber(account?.margin_used);
  const latestWallet = finiteNumber(latest?.wallet_balance);
  const walletBalance = latestWallet
    ?? (accountCash != null && accountMargin != null ? accountCash + accountMargin : null);
  const availableBalance = finiteNumber(account?.cash_available ?? latest?.available_balance ?? s.book?.cash_remaining);
  const reservedMargin = finiteNumber(account?.margin_used ?? latest?.reserved_margin);

  const livePositionValues = Object.values(latest?.position_values ?? {})
    .map((value) => finiteNumber(value))
    .filter((value): value is number => value !== null);
  const openNotional = finiteNumber(latest?.open_notional)
    ?? (livePositionValues.length > 0 ? livePositionValues.reduce((sum, value) => sum + Math.abs(value), 0) : null);

  const unrealizedPnl = finiteNumber(account?.unrealized_pnl ?? latest?.unrealized_pnl);
  const realizedPnl = finiteNumber(account?.realized_pnl ?? overall?.realized_pnl);
  const feesPaid = finiteNumber(account?.fees ?? overall?.fees_paid);
  const accountFunding = finiteNumber(account?.funding_pnl);
  const latestFundingPaid = finiteNumber(latest?.funding_paid);
  const fundingPnl = accountFunding ?? (latestFundingPaid != null ? -latestFundingPaid : null);

  const configuredLeverage = account?.leverage ?? s.session.risk_config?.leverage;
  const isLeveraged = isAllowedLeverage(configuredLeverage);
  const leveragedCount = positions.filter((position) => position.leverage !== "1x").length;
  const marginUsagePct = walletBalance != null && walletBalance > 0 && reservedMargin != null
    ? reservedMargin / walletBalance
    : null;

  const numericNetPnls = closedTrades
    .map((trade) => Number(trade.netPnl.replace(/[^0-9.-]/g, "")))
    .filter(Number.isFinite);
  const winningPnls = numericNetPnls.filter((value) => value > 0);
  const losingPnls = numericNetPnls.filter((value) => value < 0);
  const largestWin = winningPnls.length ? Math.max(...winningPnls) : null;
  const largestLoss = losingPnls.length ? Math.min(...losingPnls) : null;

  const pnlSummary = [
    { label: "Total Closed Trades", value: String(s.trade_count), color: "text-gray-400" },
    { label: "Win Rate", value: overall?.win_rate != null ? `${(overall.win_rate * 100).toFixed(2)}%` : "—", color: "text-emerald-400" },
    { label: "Profit Factor", value: overall?.profit_factor != null ? overall.profit_factor.toFixed(2) : "—", color: overall?.profit_factor != null && overall.profit_factor >= 1 ? "text-emerald-400" : "text-red-400" },
    { label: "Total Net P&L", value: signedMoneyOrDash(realizedPnl), color: valueColor(realizedPnl) },
    { label: "Average Win", value: overall?.avg_win != null ? usd(overall.avg_win) : "—", color: "text-emerald-400" },
    { label: "Average Loss", value: overall?.avg_loss != null ? `-${usd(overall.avg_loss)}` : "—", color: "text-red-400" },
    { label: "Largest Win", value: largestWin != null ? usd(largestWin) : "—", color: "text-emerald-400" },
    { label: "Largest Loss", value: largestLoss != null ? usd(largestLoss) : "—", color: "text-red-400" },
  ];

  const curveRows = (s.equity_curve ?? [])
    .map((point) => ({ equity: finiteNumber(point.equity), time: point.time }))
    .filter((point): point is { equity: number; time: string } => point.equity !== null);
  const curvePoints = curveRows.map((point) => point.equity);
  const curveLabelsRaw = curveRows.map((point) => point.time);
  const curveLabels = curveLabelsRaw.length
    ? [0, 0.33, 0.66, 1].map((fraction) => {
      const idx = Math.min(curveLabelsRaw.length - 1, Math.round(fraction * (curveLabelsRaw.length - 1)));
      const date = new Date(curveLabelsRaw[idx]);
      return Number.isFinite(date.getTime())
        ? date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
        : "—";
    })
    : [];

  return (
    <div className="min-h-screen bg-[#0a0e17] p-5 text-gray-100">
      {error && sessions && (
        <div className="mb-4 rounded-lg border border-amber-800 bg-amber-950/40 p-3 text-sm text-amber-300">
          Live refresh failed: {error}. Last valid dashboard update: {formatAge(lastSuccessfulRefreshMs === null ? null : nowMs - lastSuccessfulRefreshMs)}.
          Existing values are retained and marked stale; they were not replaced with zeroes.
        </div>
      )}
      <WorkerStatusGrid
        sessions={sessions ?? []}
        nowMs={nowMs}
        decisionByWorker={decisionByWorker}
        selectedSessionId={selectedSessionId}
        onSelect={(w) => { userPickedTabRef.current = true; setActiveTab(classifySessionTab(w)); setSelectedSessionId(w.session_id); }}
      />
      <OperationsSummary
        sessions={sessions}
        providerHealth={providerHealth}
        providerHealthError={providerHealthError}
        decisionHealth={decisionHealth}
        refreshAgeMs={lastSuccessfulRefreshMs === null ? null : nowMs - lastSuccessfulRefreshMs}
        nowMs={nowMs}
      />
      <ProviderHealthBar health={providerHealth} error={providerHealthError} active={market.source} nowMs={nowMs} />

      {/* Tabs + session picker */}
      <div className="mb-4 flex items-center gap-2 flex-wrap">
        {(Object.keys(TAB_LABELS) as PaperTab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => { userPickedTabRef.current = true; setActiveTab(tab); setSelectedSessionId(null); }}
            className={cn(
              "px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors",
              tab === activeTab ? "border-blue-500 bg-blue-500/10 text-blue-400" : "border-gray-800 text-gray-500 hover:text-gray-300",
            )}
          >
            {TAB_LABELS[tab]} ({tabCounts[tab]})
          </button>
        ))}
        {visibleSessions.length > 1 && (
          <div className="flex items-center gap-1.5 ml-2 flex-wrap">
            {visibleSessions.map((sess) => (
              <button
                key={sess.session_id}
                onClick={() => setSelectedSessionId(sess.session_id)}
                className={cn(
                  "px-2 py-1 rounded-full text-xs border",
                  sess.session_id === s.session_id ? "border-emerald-500 text-emerald-400 bg-emerald-500/10" : "border-gray-800 text-gray-500",
                )}
              >
              {sessionDisplayName(sess)}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Header */}
      <div className="mb-5 flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Live Paper Trading Dashboard</h1>
          <div className="mt-2"><MarketRoute active={market.source} /></div>
          <div className="mt-1 flex items-center gap-3 text-sm flex-wrap">
            <span className="text-gray-500">Session:</span>
          <span className="font-semibold text-emerald-400">{sessionDisplayName(s)}</span>
            <span className="rounded bg-blue-900/40 px-2 py-0.5 text-xs text-blue-400 border border-blue-800">
              {isLeveraged ? `${s.session.risk_config?.margin_mode ?? "isolated"} · ${configuredLeverage}x · ${leveragedCount} open` : "Invalid leverage configuration"}
            </span>
            <span className="text-gray-500">
              Update: {latest ? new Date(latest.timestamp).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"}
            </span>
            <span className={cn(
              "inline-block h-2 w-2 rounded-full",
              s.status === "running" && selectedHeartbeatFresh
                ? "bg-emerald-500"
                : selectedHeartbeatAgeMs !== null
                  ? "bg-amber-500"
                  : "bg-gray-600",
            )} />
            <span className="text-gray-500">
              {s.status} · heartbeat {formatAge(selectedHeartbeatAgeMs)}
            </span>
            {s.classification === "archived" && (
              <span className="rounded bg-amber-900/40 px-2 py-0.5 text-xs text-amber-400 border border-amber-800">Archived</span>
            )}
            <span className="text-gray-500">Execution: PAPER</span>
            {account && <span className="text-gray-400">Strategy: {account.strategy_id}</span>}
            {account && <span className="text-gray-400">Timeframe: {account.timeframe}</span>}
            {account && <span className="text-gray-400">Worker: {account.worker_id}</span>}
            {account && <span className="text-blue-400">Leverage: {account.leverage}x</span>}
            {account && <span className={account.ledger_status === "OK" ? "text-emerald-400" : "text-red-400"}>Ledger: {account.ledger_status}</span>}
            <span className="inline-flex items-center gap-1.5 text-gray-500">
              Market source <MarketSourceBadge source={market.source} rawSource={market.rawSource} status={market.status} />
              <span className={market.status === "OK" ? "text-emerald-400" : market.status === "STALE" ? "text-amber-400" : "text-red-400"}>{market.status}</span>
            </span>
            <span className="text-gray-500">
              Market mark: {market.observedAt ? new Date(market.observedAt).toLocaleString() : "not supplied"} · {formatAge(market.ageMs)}
            </span>
            <span className="text-gray-500">
              Dashboard refresh: {lastSuccessfulRefreshMs ? new Date(lastSuccessfulRefreshMs).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "pending"} · {formatAge(lastSuccessfulRefreshMs === null ? null : nowMs - lastSuccessfulRefreshMs)}
            </span>
            <span className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-400 border border-gray-700">Poll 5s</span>
          </div>
        </div>
      </div>

      {/* Stats Row */}
      <div className="mb-5 grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-9 gap-3">
        <StatCard label="Initial Capital" value={moneyOrDash(initialCapital)} sub="Configured paper capital" />
        <StatCard label="Wallet Balance" value={moneyOrDash(walletBalance)} sub={`Available: ${moneyOrDash(availableBalance)}`} />
        <StatCard
          label="Reserved Margin"
          value={moneyOrDash(reservedMargin)}
          sub={`In Use: ${leveragedCount} Position${leveragedCount === 1 ? "" : "s"}`}
          valueColor={reservedMargin == null ? "text-gray-500" : "text-orange-400"}
        />
        <StatCard label="Open Notional" value={moneyOrDash(openNotional)} sub="From current marked positions" />
        <StatCard
          label="Unrealized P&L"
          value={signedMoneyOrDash(unrealizedPnl)}
          sub={equity != null && equity !== 0 && unrealizedPnl != null ? pctSigned(unrealizedPnl / equity) : "—"}
          valueColor={valueColor(unrealizedPnl)}
          glow
        />
        <StatCard label="Realized P&L" value={signedMoneyOrDash(realizedPnl)} sub="All Time" valueColor={valueColor(realizedPnl)} glow />
        <StatCard label="Fees Paid" value={moneyOrDash(feesPaid)} sub="All Time" />
        <StatCard label="Funding (Net)" value={signedMoneyOrDash(fundingPnl)} sub="Account Scoped" valueColor={valueColor(fundingPnl)} />
        <StatCard label="Current Equity" value={moneyOrDash(equity)} sub={signedPctOrDash(pnlPct)} valueColor={valueColor(pnl)} glow />
      </div>

      {account && (
        <div className="mb-5 grid grid-cols-1 md:grid-cols-3 gap-3 text-xs text-gray-400">
          <div className="rounded-lg border border-gray-800 bg-gray-900/70 p-3">Last heartbeat: {account.last_heartbeat ? new Date(account.last_heartbeat).toLocaleString() : "—"}</div>
          <div className="rounded-lg border border-gray-800 bg-gray-900/70 p-3">Last trade: {account.last_trade ? new Date(account.last_trade).toLocaleString() : s.trade_count > 0 ? "Timestamp unavailable" : "No closed trades recorded"}</div>
          <div className="rounded-lg border border-gray-800 bg-gray-900/70 p-3">Risk state: {account.risk_state && Object.keys(account.risk_state).length ? JSON.stringify(account.risk_state) : "Normal"}</div>
        </div>
      )}

      {/* Open Positions Table */}
      <div className="mb-5 rounded-xl border border-gray-800 bg-gray-900/80 p-5">
        <h2 className="mb-4 text-lg font-bold text-white">
          Open Positions <span className="text-gray-500">({positions.length})</span>
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs font-semibold uppercase tracking-wider text-gray-500">
                <th className="pb-3 pr-4">Symbol</th>
                <th className="pb-3 pr-4">Side</th>
                <th className="pb-3 pr-4">Margin (USDT)</th>
                <th className="pb-3 pr-4">Leverage</th>
                <th className="pb-3 pr-4">Notional (USDT)</th>
                <th className="pb-3 pr-4">Entry Price</th>
                <th className="pb-3 pr-4">Mark Price</th>
                <th className="pb-3 pr-4">Liq. Price</th>
                <th className="pb-3 pr-4">TP / SL</th>
                <th className="pb-3 pr-4">Unrealized P&L (USDT)</th>
                <th className="pb-3 pr-4">ROI (Margin %)</th>
                <th className="pb-3">Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {positions.length === 0 && (
                <tr><td colSpan={12} className="py-6 text-center text-gray-600">No open positions right now.</td></tr>
              )}
              {positions.map((pos, i) => (
                <tr key={i} className="group hover:bg-gray-800/50 transition-colors">
                  <td className="py-3 pr-4">
                    <div className="font-bold text-white">{pos.symbol}</div>
                    <div className="text-xs text-gray-600">{pos.perp}</div>
                  </td>
                  <td className="py-3 pr-4"><SideBadge side={pos.side} /></td>
                  <td className="py-3 pr-4 text-gray-400">{pos.margin}</td>
                  <td className="py-3 pr-4 font-semibold text-blue-400">{pos.leverage}</td>
                  <td className="py-3 pr-4 text-gray-400">{pos.notional}</td>
                  <td className="py-3 pr-4 text-gray-400">{pos.entryPrice}</td>
                  <td className="py-3 pr-4 font-semibold text-white">{pos.markPrice}</td>
                  <td className="py-3 pr-4 text-red-500">{pos.liqPrice}</td>
                  <td className="py-3 pr-4">
                    {pos.tp && <div className="text-xs text-emerald-400">{pos.tp}</div>}
                    {pos.sl && <div className="text-xs text-red-500">{pos.sl}</div>}
                    {!pos.tp && !pos.sl && <span className="text-gray-600">—</span>}
                  </td>
                  <td className={cn("py-3 pr-4 font-bold", pos.unrealizedPnl === "—" ? "text-gray-600" : isPositive(pos.unrealizedPnl) ? "text-emerald-400" : "text-red-400")}>
                    {pos.unrealizedPnl}
                  </td>
                  <td className={cn("py-3 pr-4 font-bold", pos.roi === "—" ? "text-gray-600" : isPositive(pos.roi) ? "text-emerald-400" : "text-red-400")}>
                    {pos.roi}
                  </td>
                  <td className="py-3 pr-4 text-gray-400">{pos.duration}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Bottom Cards Row */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
        <div className="rounded-xl border border-gray-800 bg-gray-900/80 p-5">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-base font-bold text-white">Equity Curve</h3>
          </div>
          <EquityCurve points={curvePoints} labels={curveLabels} currentEquity={equity} changePct={pnlPct} />
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-900/80 p-5">
          <h3 className="mb-4 text-base font-bold text-white">Margin Allocation</h3>
          <DonutChart data={marginAlloc} total={usd(totalMargin)} />
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-900/80 p-5">
          <h3 className="mb-4 text-base font-bold text-white">
            P&L Summary <span className="text-sm font-normal text-gray-500">(All Time)</span>
          </h3>
          <div className="flex flex-col gap-3">
            {pnlSummary.map((item, i) => (
              <div key={i} className="flex items-center justify-between">
                <span className="text-sm text-gray-500">{item.label}</span>
                <span className={cn("text-sm font-semibold", item.color)}>{item.value}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-900/80 p-5">
          <h3 className="mb-4 text-base font-bold text-white">Account Health</h3>
          <HealthRing marginUsagePct={marginUsagePct} leveraged={isLeveraged} />
        </div>
      </div>

      {/* Recent Closed Trades */}
      <div className="mt-5 rounded-xl border border-gray-800 bg-gray-900/80 p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-white">
            Recent Closed Trades <span className="text-gray-500">({closedTrades.length})</span>
          </h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs font-semibold uppercase tracking-wider text-gray-500">
                <th className="pb-3 pr-4">Time</th>
                <th className="pb-3 pr-4">Symbol</th>
                <th className="pb-3 pr-4">Side</th>
                <th className="pb-3 pr-4">Origin</th>
                <th className="pb-3 pr-4">Margin</th>
                <th className="pb-3 pr-4">Lev.</th>
                <th className="pb-3 pr-4">Notional</th>
                <th className="pb-3 pr-4">Entry Price</th>
                <th className="pb-3 pr-4">Exit Price</th>
                <th className="pb-3 pr-4">Exit Reason</th>
                <th className="pb-3 pr-4">Gross P&L</th>
                <th className="pb-3 pr-4">Fees</th>
                <th className="pb-3 pr-4">Funding</th>
                <th className="pb-3 pr-4">Net P&L</th>
                <th className="pb-3">ROI (Margin %)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {closedTrades.length === 0 && (
                <tr><td colSpan={15} className="py-6 text-center text-gray-600">No closed trades yet.</td></tr>
              )}
              {closedTrades.map((trade, i) => (
                <tr key={i} className="hover:bg-gray-800/50 transition-colors">
                  <td className="py-3 pr-4 text-gray-400">{trade.time}</td>
                  <td className="py-3 pr-4 font-bold text-white">{trade.symbol}</td>
                  <td className="py-3 pr-4"><SideBadge side={trade.side} /></td>
                  <td className="py-3 pr-4">
                    <span className={cn(
                      "rounded px-1.5 py-0.5 text-xs border",
                      trade.origin === "PAPER_BOOTSTRAP" ? "border-amber-800 bg-amber-900/30 text-amber-400" : "border-gray-700 bg-gray-800 text-gray-400",
                    )}>
                      {trade.origin}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-gray-400">{trade.margin}</td>
                  <td className="py-3 pr-4 text-blue-400">{trade.leverage}</td>
                  <td className="py-3 pr-4 text-gray-400">{trade.notional}</td>
                  <td className="py-3 pr-4 text-gray-400">{trade.entryPrice}</td>
                  <td className="py-3 pr-4 text-gray-400">{trade.exitPrice}</td>
                  <td className={cn("py-3 pr-4 text-xs", /take.?profit|target/i.test(trade.exitReason) ? "text-emerald-400" : /stop.?loss|liquidation/i.test(trade.exitReason) ? "text-red-400" : "text-gray-400")}>
                    {trade.exitReason}
                  </td>
                  <td className={cn("py-3 pr-4", trade.grossPnl === "—" ? "text-gray-600" : isPositive(trade.grossPnl) ? "text-emerald-400" : "text-red-400")}>
                    {trade.grossPnl}
                  </td>
                  <td className="py-3 pr-4 text-red-500">{trade.fees}</td>
                  <td className="py-3 pr-4 text-gray-400">{trade.funding}</td>
                  <td className={cn("py-3 pr-4 font-bold", trade.netPnl === "—" ? "text-gray-600" : isPositive(trade.netPnl) ? "text-emerald-400" : "text-red-400")}>
                    {trade.netPnl}
                  </td>
                  <td className={cn("py-3 font-bold", trade.roi === "—" ? "text-gray-600" : isPositive(trade.roi) ? "text-emerald-400" : "text-red-400")}>
                    {trade.roi}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
