import { authHeaders, withAuthQuery } from "@/lib/apiAuth";

const BASE = "";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_REQUIRED_MESSAGE =
  "Remote API access requires an API key. Add it in Settings, or run the backend on localhost for local-only use.";

export function isAuthRequiredError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    detail = body.detail || body.message || detail;
  } catch { /* ignore */ }
  if (res.status === 401 || res.status === 403) {
    detail = AUTH_REQUIRED_MESSAGE;
  }
  return new ApiError(detail, res.status);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const mergedHeaders: Record<string, string> = { "Content-Type": "application/json", ...authHeaders() };
  if (headers) {
    new Headers(headers).forEach((value, key) => {
      mergedHeaders[key] = value;
    });
  }
  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: mergedHeaders,
  });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  const text = await res.text();
  if (!text) return {} as T;

  const contentType = res.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const preview = text.slice(0, 80).replace(/\s+/g, " ");
    throw new ApiError(
      `Expected JSON from ${path}, got ${contentType || "unknown content type"}: ${preview}`,
      res.status,
    );
  }

  return JSON.parse(text) as T;
}

export interface UploadResult {
  status: string;
  file_path: string;
  filename: string;
}

async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/upload`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

function appendQueryParam(url: string, key: string, value: string): string {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
}

export const api = {
  uploadFile,
  listRuns: (limit?: number) => request<RunListItem[]>(`/runs${limit ? `?limit=${encodeURIComponent(String(limit))}` : ""}`),
  listAutopilotEvidenceRuns: (limit?: number) =>
    request<AutopilotEvidenceRunItem[]>(`/autopilot-runs${limit ? `?limit=${encodeURIComponent(String(limit))}` : ""}`),
  getRun: (id: string, params: RunDetailParams = {}) => {
    const q = new URLSearchParams();
    if (params.chart_payload) q.set("chart_payload", params.chart_payload);
    if (params.chart_symbol) q.set("chart_symbol", params.chart_symbol);
    const qs = q.toString();
    return request<RunData>(`/runs/${id}${qs ? `?${qs}` : ""}`);
  },
  getRunCode: (id: string) => request<Record<string, string>>(`/runs/${id}/code`),
  getRunPine: (id: string) => request<PineScriptResult>(`/runs/${id}/pine`),
  listSessions: () => request<SessionItem[]>("/sessions"),
  createSession: (title?: string) => request<SessionItem>("/sessions", { method: "POST", body: JSON.stringify({ title: title || "" }) }),
  deleteSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "DELETE" }),
  renameSession: (sid: string, title: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  sendMessage: (sid: string, content: string) => request<{ message_id: string; attempt_id: string }>(`/sessions/${sid}/messages`, { method: "POST", body: JSON.stringify({ content }) }),
  cancelSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}/cancel`, { method: "POST" }),
  getSessionMessages: (sid: string) => request<MessageItem[]>(`/sessions/${sid}/messages`),
  createGoal: (sid: string, body: CreateGoalRequest) =>
    request<GoalSnapshot>(`/sessions/${sid}/goal`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getGoal: (sid: string) => request<GoalSnapshot | null>(`/sessions/${sid}/goal`),
  updateGoal: (sid: string, body: UpdateGoalRequest) =>
    request<UpdateGoalResponse>(`/sessions/${sid}/goal`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  addGoalEvidence: (sid: string, body: AddGoalEvidenceRequest) =>
    request<AddGoalEvidenceResponse>(`/sessions/${sid}/goal/evidence`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateGoalStatus: (sid: string, body: UpdateGoalStatusRequest) =>
    request<UpdateGoalStatusResponse>(`/sessions/${sid}/goal/status`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  sseUrl: (sid: string, options?: { replay?: "active" }) => {
    let url = withAuthQuery(`${BASE}/sessions/${sid}/events`);
    if (options?.replay) url = appendQueryParam(url, "replay", options.replay);
    return url;
  },

  // Swarm API
  listSwarmPresets: () => request<SwarmPreset[]>("/swarm/presets"),
  createSwarmRun: (preset_name: string, user_vars: Record<string, string>) =>
    request<{ id: string; status: string }>("/swarm/runs", {
      method: "POST",
      body: JSON.stringify({ preset_name, user_vars }),
    }),
  listSwarmRuns: () => request<SwarmRunSummary[]>("/swarm/runs"),
  getSwarmRun: (id: string) => request<Record<string, unknown>>(`/swarm/runs/${id}`),
  swarmSseUrl: (id: string) => withAuthQuery(`${BASE}/swarm/runs/${id}/events`),
  cancelSwarmRun: (id: string) =>
    request<{ status: string }>(`/swarm/runs/${id}/cancel`, { method: "POST" }),
  retrySwarmRun: (id: string) =>
    request<{ id: string; status: string; preset_name: string }>(`/swarm/runs/${id}/retry`, { method: "POST" }),
  getLLMSettings: () => request<LLMSettings>("/settings/llm"),
  updateLLMSettings: (settings: UpdateLLMSettingsRequest) =>
    request<LLMSettings>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getDataSourceSettings: () => request<DataSourceSettings>("/settings/data-sources"),
  updateDataSourceSettings: (settings: UpdateDataSourceSettingsRequest) =>
    request<DataSourceSettings>("/settings/data-sources", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getChannelStatus: () => request<ChannelRuntimeStatus>("/channels/status"),
  startChannels: () => request<ChannelRuntimeActionResponse>("/channels/start", { method: "POST" }),
  stopChannels: () => request<ChannelRuntimeActionResponse>("/channels/stop", { method: "POST" }),
  runChannelPairingCommand: (body: ChannelPairingCommandRequest) =>
    request<ChannelPairingCommandResponse>("/channels/pairing/command", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Alpha Zoo API
  listAlphas: (params: AlphaListParams = {}) => {
    const q = new URLSearchParams();
    if (params.zoo) q.set("zoo", params.zoo);
    if (params.theme) q.set("theme", params.theme);
    if (params.universe) q.set("universe", params.universe);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<AlphaListResponse>(`/alpha/list${qs ? `?${qs}` : ""}`);
  },
  getAlpha: (alphaId: string) =>
    request<AlphaDetailResponse>(`/alpha/${encodeURIComponent(alphaId)}`),
  createAlphaBench: (body: AlphaBenchRequest) =>
    request<{ status: string; job_id: string }>("/alpha/bench", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaBenchStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/alpha/bench/${encodeURIComponent(jobId)}/stream`),
  createAlphaCompare: (body: AlphaCompareRequest) =>
    request<{ status: string; job_id: string }>("/alpha/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaCompareStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/alpha/compare/${encodeURIComponent(jobId)}/stream`),

  // Connector runtime channel — privileged surface actions (NOT agent tools).
  // commit is the ONLY action that writes a mandate; halt trips the kill switch.
  commitMandate: (body: CommitMandateRequest) =>
    request<CommitMandateResponse>("/mandate/commit", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  haltLive: (session_id?: string, broker?: string, reason?: string) =>
    request<HaltLiveResponse>("/live/halt", {
      method: "POST",
      body: JSON.stringify({ session_id, broker, reason }),
    }),
  // Read the persistent runtime status across all authorized brokers (SPEC §7.5).
  // Polled by the RunnerStatus panel; a plain authenticated GET, never a chat message.
  getLiveStatus: (signal?: AbortSignal) => request<LiveStatus>("/live/status", { signal }),
  authorizeLive: (broker: string) =>
    request<LiveAuthorizeResponse>("/live/authorize", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  // Start/stop the persistent runner (SPEC §7.5). Privileged surface actions, not agent tools.
  startLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/start", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  stopLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/stop", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),

  // Governed backtest — deterministic, no ReAct/chat model in the loop.
  runGovernedBacktest: (body: GovernedBacktestRequest) =>
    request<GovernedBacktestResponse>("/backtest/governed", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Paper sessions — read-only; simulated live paper trading, not a real broker.
  listPaperSessions: (scope?: PaperSessionScope) =>
    request<PaperSessionSummary[]>(`/paper-sessions${scope ? `?scope=${scope}` : ""}`),
  getPaperProviderHealth: () =>
    request<PaperProviderHealth>("/paper-sessions/provider-health"),
  getPaperDecisionHealth: () =>
    request<PaperDecisionHealth>("/paper-sessions/decision-health"),
  getPaperSession: (sessionId: string) =>
    request<PaperSessionSummary>(`/paper-sessions/${encodeURIComponent(sessionId)}`),
  getPaperSessionLivePrices: (sessionId: string) =>
    request<PaperSessionLivePrices>(`/paper-sessions/${encodeURIComponent(sessionId)}/live-prices`),
  getShadowComparison: () => request<ShadowComparisonRow[]>("/paper-sessions/shadow-comparison"),
  getPaperTradingNotifications: (after?: string) => request<Array<{ id: string; title: string; message: string; severity: string; created_at: string }>>(`/paper-trading/notifications${after ? `?after=${encodeURIComponent(after)}` : ""}`),
};

// --- Swarm types ---

export interface SwarmPreset {
  name: string;
  title: string;
  description: string;
  agent_count: number;
  variables: { name: string; description: string; required: boolean }[];
}

export interface SwarmRunSummary {
  id: string;
  preset_name: string;
  status: string;
  created_at: string;
  task_count: number;
  completed_count: number;
}

export interface LLMProviderOption {
  name: string;
  label: string;
  api_key_env?: string | null;
  base_url_env: string;
  default_model: string;
  default_base_url: string;
  api_key_required: boolean;
  auth_type?: string;
  login_command?: string | null;
}

export interface LLMSettings {
  provider: string;
  model_name: string;
  base_url: string;
  api_key_env?: string | null;
  api_key_configured: boolean;
  api_key_hint?: string | null;
  api_key_required: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
  sse_timeout_seconds: number;
  env_path: string;
  providers: LLMProviderOption[];
}

export interface UpdateLLMSettingsRequest {
  provider: string;
  model_name: string;
  base_url: string;
  api_key?: string;
  clear_api_key?: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort?: string;
}

export interface DataSourceSettings {
  tushare_token_configured: boolean;
  tushare_token_hint?: string | null;
  baostock_supported: boolean;
  baostock_installed: boolean;
  baostock_message: string;
  env_path: string;
}

export interface UpdateDataSourceSettingsRequest {
  tushare_token?: string;
  clear_tushare_token?: boolean;
}

export interface ChannelAdapterStatus {
  name: string;
  display_name: string;
  configured: boolean;
  enabled: boolean;
  available: boolean;
  loaded: boolean;
  running: boolean;
  error?: string;
  install_hint?: string;
}

export interface ChannelRuntimeStatus {
  running: boolean;
  inbound_queue: number;
  outbound_queue: number;
  session_count: number;
  channels: Record<string, ChannelAdapterStatus>;
}

export interface ChannelRuntimeActionResponse extends ChannelRuntimeStatus {
  status: string;
}

export interface ChannelPairingCommandRequest {
  channel: string;
  command: string;
}

export interface ChannelPairingCommandResponse {
  channel: string;
  reply: string;
}

// --- Types matching backend API contracts ---

export interface RunListItem {
  run_id: string;
  status: string;
  created_at: string;
  prompt?: string;
  total_return?: number;
  sharpe?: number;
  codes?: string[];
  start_date?: string;
  end_date?: string;
}

export interface RunDetailParams {
  chart_payload?: "summary";
  chart_symbol?: string;
}

export interface AutopilotEvidenceRunItem {
  run_dir: string;
  strategy_implementation_status: string;
  provenance_valid: boolean;
  has_trades: boolean;
  statistically_evaluable: boolean;
  hypothesis_supported: boolean | null;
  run_purpose: string;
  trade_count?: number;
  total_return?: number;
  sharpe?: number;
  generated_at?: string;
  signal_engine_sha256?: string;
}

export interface PriceBar {
  time: string;
  timestamp?: string;
  code?: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  time: string;
  timestamp?: string;
  code?: string;
  side: "BUY" | "SELL";
  price: number;
  qty?: number;
  reason?: string;
  text?: string;
}

export interface EquityPoint {
  time: string;
  equity: string | number;
  drawdown: string | number;
}

export interface ValidationData {
  monte_carlo?: {
    actual_sharpe: number;
    actual_max_dd: number;
    p_value_sharpe: number;
    p_value_max_dd: number;
    simulated_sharpe_mean: number;
    simulated_sharpe_std: number;
    simulated_sharpe_p5: number;
    simulated_sharpe_p95: number;
    n_simulations: number;
    n_trades: number;
    error?: string;
  };
  bootstrap?: {
    observed_sharpe: number;
    ci_lower: number;
    ci_upper: number;
    median_sharpe: number;
    prob_positive: number;
    confidence: number;
    n_bootstrap: number;
    error?: string;
  };
  walk_forward?: {
    n_windows: number;
    windows: Array<{
      window: number;
      start: string;
      end: string;
      return: number;
      sharpe: number;
      max_dd: number;
      trades: number;
      win_rate: number;
    }>;
    profitable_windows: number;
    consistency_rate: number;
    return_mean: number;
    return_std: number;
    sharpe_mean: number;
    sharpe_std: number;
    error?: string;
  };
}

export interface RunData {
  status: string;
  run_id: string;
  prompt?: string;
  elapsed_seconds?: number;
  run_directory?: string;
  run_stage?: string;
  run_context?: Record<string, unknown>;

  metrics?: BacktestMetrics;
  artifacts?: ArtifactInfo[];
  run_card?: RunCard;
  validation?: ValidationData;

  chart_symbols?: string[];
  price_series?: Record<string, PriceBar[]>;
  indicator_series?: Record<string, Record<string, IndicatorPoint[]>>;
  trade_markers?: TradeMarker[];
  equity_curve?: EquityPoint[];
  trade_log?: Array<Record<string, string>>;
  run_logs?: Array<{ source?: string; line_number?: number; message?: string }>;
}

export interface RunCard {
  schema_version?: string;
  generated_at?: string;
  run_dir?: string;
  backtest?: Record<string, unknown>;
  reproducibility?: Record<string, unknown>;
  data_sources?: string[];
  metrics?: Record<string, unknown>;
  validation?: unknown;
  warnings?: string[];
  artifacts?: RunCardArtifact[];
  [key: string]: unknown;
}

export interface RunCardArtifact {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface BacktestMetrics {
  final_value: number;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  [key: string]: number;
}


export interface IndicatorPoint {
  time: string;
  value: number;
}

export interface ArtifactInfo {
  name: string;
  path: string;
  type: string;
  size: number;
  exists: boolean;
}

export interface PineScriptResult {
  exists: boolean;
  content: string | null;
}

export interface SessionItem {
  session_id: string;
  title?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  last_attempt_id?: string;
}

// --- Goal types ---

export type GoalStatus =
  | "active"
  | "paused"
  | "waiting_user"
  | "needs_refresh"
  | "insufficient_evidence"
  | "compliance_blocked"
  | "blocked"
  | "budget_limited"
  | "usage_limited"
  | "complete"
  | "cancelled"
  | "superseded";

export type GoalRiskTier =
  | "research_general"
  | "market_specific_short_term"
  | "personalized_advice_or_position_sizing";

export interface GoalRecord {
  goal_id: string;
  session_id: string;
  status: GoalStatus;
  objective: string;
  ui_summary: string;
  source: string;
  protocol: string;
  risk_tier: GoalRiskTier;
  token_budget?: number | null;
  tokens_used: number;
  turn_budget?: number | null;
  turns_used: number;
  time_budget_seconds?: number | null;
  time_used_seconds: number;
  budget_wrapup_sent: boolean;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  recap?: string | null;
}

export interface GoalClaim {
  claim_id: string;
  goal_id: string;
  session_id: string;
  claim_type: string;
  text: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface GoalCriterion {
  criterion_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  required: boolean;
  status: string;
  freshness_requirement?: string | null;
  protocol_step?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GoalEvidence {
  evidence_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe: string[];
  benchmark: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  retrieved_at: string;
  data_as_of?: string | null;
  freshness_status: string;
  verification_status: string;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids: string[];
  created_at: string;
}

export interface GoalSnapshot {
  goal: GoalRecord;
  claims: GoalClaim[];
  criteria: GoalCriterion[];
  evidence: GoalEvidence[];
  evidence_count: number;
}

export interface CreateGoalRequest {
  objective: string;
  criteria?: string[];
  ui_summary?: string;
  protocol?: string;
  risk_tier?: GoalRiskTier;
  token_budget?: number;
  turn_budget?: number;
  time_budget_seconds?: number;
}

export interface AddGoalEvidenceRequest {
  goal_id: string;
  expected_goal_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type?: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe?: string[];
  benchmark?: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions?: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  data_as_of?: string | null;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids?: string[];
}

export interface UpdateGoalRequest {
  goal_id: string;
  expected_goal_id: string;
  objective?: string;
  ui_summary?: string;
}

export interface UpdateGoalResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

export interface AddGoalEvidenceResponse {
  evidence: GoalEvidence;
  snapshot: GoalSnapshot;
}

export interface GoalAuditRowRequest {
  criterion_id: string;
  result: string;
  evidence_ids?: string[];
  notes?: string;
}

export interface UpdateGoalStatusRequest {
  goal_id: string;
  expected_goal_id: string;
  status: GoalStatus;
  audit?: GoalAuditRowRequest[];
  recap?: string | null;
}

export interface UpdateGoalStatusResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

// --- Alpha Zoo types ---

export interface AlphaListParams {
  zoo?: string;
  theme?: string;
  universe?: string;
  limit?: number;
}

export interface AlphaSummary {
  id: string;
  zoo: string;
  theme: string[];
  universe: string[];
  nickname?: string;
  decay_horizon?: number | null;
  min_warmup_bars?: number | null;
  requires_sector?: boolean;
}

export interface AlphaListResponse {
  status: string;
  alphas: AlphaSummary[];
  total: number;
  returned: number;
  truncated: boolean;
}

export interface AlphaDetail {
  id: string;
  zoo: string;
  module_path?: string;
  meta: Record<string, unknown>;
}

export interface AlphaDetailResponse {
  status: string;
  alpha: AlphaDetail;
  source_code: string;
}

export interface AlphaBenchRequest {
  zoo: string;
  universe: string;
  period: string;
  top?: number;
}

export interface AlphaBenchTopRow {
  id: string;
  ic_mean: number;
  ir: number;
  theme: string[];
  formula_latex: string;
  category: "alive" | "reversed" | "dead";
}

export interface AlphaBenchResult {
  alive: number;
  reversed: number;
  dead: number;
  skipped?: number;
  top5_by_ir: AlphaBenchTopRow[];
  dead_examples: AlphaBenchTopRow[];
  by_theme: Record<string, { alive: number; reversed: number; dead: number }>;
}

export interface AlphaCompareRequest {
  alpha_ids: string[];
  universe: string;
  period: string;
  /** One of: ir | ic_mean | ic_positive_ratio | ic_count (default ir). */
  sort?: string;
}

export interface AlphaCompareRow {
  rank: number;
  id: string;
  zoo: string;
  ic_mean: number;
  ic_std: number;
  ir: number;
  ic_positive_ratio: number;
  ic_count: number;
  /** `delta_<sort>_vs_best` — gap to the top-ranked alpha on the active metric. */
  [deltaKey: string]: number | string;
}

export interface AlphaCompareSkip {
  id: string;
  reason: string;
}

export interface AlphaCompareResult {
  universe: string;
  period: string;
  sort: string;
  n_compared: number;
  n_skipped: number;
  winner: string;
  ranking: AlphaCompareRow[];
  skipped: AlphaCompareSkip[];
}

// --- Connector runtime channel types ---

/** One mandate profile inside a `mandate.proposal` event (SPEC Consent §1). */
export interface MandateProfile {
  ordinal: number;
  label: string;
  /** Concrete ticker list, or a structural universe descriptor (e.g. "tech_sector"). */
  universe: string[] | string;
  max_order_usd: number;
  daily_trade_cap: number;
  /** "none" for cash-only, otherwise a leverage descriptor/multiple. */
  leverage: string | number;
  instruments: string[];
  notes?: string;
}

/** Account block of a `mandate.proposal` event. */
export interface MandateProposalAccount {
  broker: string;
  type: string;
  funded_by: string;
}

/** Payload of the `mandate.proposal` SSE event (SPEC Consent §1). */
export interface MandateProposal {
  type?: string;
  proposal_id: string;
  session_id?: string;
  intent_normalized?: string;
  account?: MandateProposalAccount;
  ceilings_ref?: string;
  profiles: MandateProfile[];
  funding_note?: string;
  halt_note?: string;
  /** Present only when this proposal was triggered by a mandate breach (SPEC Consent §3). */
  reauth_for?: { breach_id?: string } | null;
}

/** Payload of the `mandate.committed` SSE event (SPEC Consent §1 COMMIT). */
export interface MandateCommitted {
  proposal_id?: string;
  mandate_id?: string;
  consent_record_id?: string;
  selected_ordinal?: number;
  broker?: string;
  /** Resolved limits, surfaced for the compact active-mandate badge. */
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

/** Payload of the `live.halted` SSE event (SPEC Consent §4). */
export interface LiveHalted {
  broker?: string | null;
  tripped_at?: string;
  by?: string;
  reason?: string;
}

/** Payload of the `live.action` SSE event (SPEC Consent §5 audit notify). */
export interface LiveAction {
  audit_id?: string;
  ts?: string;
  kind: string;
  intent_normalized?: string;
  outcome?: string;
  broker?: string;
  remote_tool?: string;
  error?: string | null;
}

export interface CommitMandateRequest {
  broker: string;
  proposal_id: string;
  selected_ordinal: number;
  /** Present only on the adjust path (SPEC Consent §3); null otherwise. */
  adjustments?: Record<string, unknown> | null;
  /** Explicit affirmative consent; the surface sets it on the user's click. */
  consent_ack: boolean;
  session_id?: string;
  account_ref?: string;
  lifetime_days?: number;
}

export interface CommitMandateResponse {
  mandate_id: string;
  consent_record_id: string;
  selected_ordinal?: number;
  broker?: string;
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

export interface HaltLiveResponse {
  halted: boolean;
  broker?: string | null;
  reason: string;
  sentinel: string;
}

export interface LiveAuthorizeRequest {
  broker: string;
}

export interface LiveAuthorizeResponse {
  broker: string;
  connector_profile: string;
  oauth_token_present: boolean;
  instruction: string;
  note?: string;
}

/** Mandate limits surfaced inside a `GET /live/status` broker entry (SPEC §7.5). */
export interface LiveMandateLimits {
  max_order_notional_usd?: number;
  max_total_exposure_usd?: number;
  max_leverage?: number;
  max_trades_per_day?: number;
  allowed_instruments?: string[];
  account_funding_usd?: number;
  [key: string]: unknown;
}

/** Active mandate block of a `GET /live/status` broker entry. */
export interface LiveMandateStatus {
  broker?: string;
  mandate_id?: string;
  account_ref?: string;
  created_at?: string;
  limits?: LiveMandateLimits;
  /** ISO timestamp the mandate auto-expires (SPEC §7.5 #7 proactive expiry). */
  expires_at?: string;
  expires_in_seconds?: number | null;
  expired?: boolean;
}

/** Runner liveness block of a `GET /live/status` broker entry (SPEC §7.5 #3). */
export interface LiveRunnerLiveness {
  broker?: string;
  alive: boolean;
  /** Unix epoch seconds of the last heartbeat tick; null if the runner never started. */
  last_tick?: number | string | null;
  last_tick_age_seconds?: number | null;
}

export interface LiveBrokerAuthStatus {
  broker: string;
  oauth_token_present: boolean;
  is_live_broker: boolean;
}

/** One broker entry in the `GET /live/status` response. */
export interface LiveBrokerStatus {
  auth: LiveBrokerAuthStatus;
  mandate?: LiveMandateStatus | null;
  runner: LiveRunnerLiveness;
  halted: boolean;
}

/** Response of `GET /live/status` (SPEC §7.5 runner status panel + C2). */
export interface LiveStatus {
  brokers: LiveBrokerStatus[];
  global_halted: boolean;
}

/** Response of `POST /live/runner/start|stop`. */
export interface LiveRunnerResponse {
  broker: string;
  started?: boolean;
  already_running?: boolean;
  stopped?: boolean;
  was_running?: boolean;
}

// --- Governed backtest types (POST /backtest/governed) ---

export interface GovernedBacktestRequest {
  symbols: string[];
  start_date: string;
  end_date: string;
  interval?: string;
  initial_cash?: number;
  source?: string;
}

export interface GovernedRunCardTruth {
  provenance_valid: boolean;
  data_source_provenance: Record<string, unknown>;
  window_integrity: boolean | null;
  statistically_evaluable: boolean;
  hypothesis_supported: boolean | null;
}

export interface GovernedBacktestResponse {
  status: "success" | "failed";
  run_id: string;
  run_dir: string;
  run_card?: GovernedRunCardTruth;
  reason?: string;
  runner_result?: Record<string, unknown>;
}

// --- Paper session types (GET /paper-sessions, read-only) ---
// Simulated periodic equal-weight rebalancing ledger marked to market
// against validated public futures prices. Not the real-money live-runner -- no broker
// order is ever placed. See agent/paper_session.py.

export interface RiskConfig {
  take_profit_pct?: number | null;
  stop_loss_pct?: number | null;
  trailing_stop_pct?: number | null;
  max_hold_hours?: number | null;
  leverage?: number;
  margin_mode?: string;
  liquidation_buffer_pct?: number;
  fixed_margin_per_trade?: number;
  portfolio_leverage?: boolean;
}

export interface PositionMetadata {
  symbol: string;
  qty: number;
  entry_price: number;
  entry_time: string;
  direction: number;
  high_water_mark: number;
  low_water_mark: number;
  leverage: number;
  margin: number;
  margin_mode: string;
  liquidation_price: number;
  stop_loss_price?: number;
  take_profit_price?: number;
}

export interface PaperSessionConfig {
  strategy_type: string;
  account_id?: string;
  strategy_id?: string | null;
  worker_id?: string | null;
  timeframe?: string | null;
  symbols: string[];
  initial_cash: number;
  entry_time: string;
  // Only fully populated for equal-weight sessions, which buy every symbol
  // up front. Funding-rate z-score sessions (start_funding_session) start
  // in 100% cash and only stamp an entry price once a symbol's signal
  // actually opens a position -- always guard a per-symbol lookup here.
  entry_prices: Partial<Record<string, number>>;
  rebalance_interval_hours?: number;
  fee_rate?: number;
  min_rebalance_notional?: number;
  source: string;
  price_kind: string;
  fees_modeled?: boolean;
  slippage_modeled?: boolean;
  cash_accounting_note?: string | null;
  risk_config?: RiskConfig;
}

export interface PaperSessionBook {
  positions: Record<string, number>;
  cash_remaining: number;
  last_rebalance_time: string;
  position_metadata?: Record<string, PositionMetadata>;
}

// FuturesPaperEngine's per-mark open-position rows (marks.jsonl "open_positions"
// array) -- the futures-engine equivalent of the spot mark's prices/
// position_values/position_pnl maps below. A futures mark carries this array
// instead of those three maps; reading only the spot fields against a
// futures mark is what left mark price/notional/uPnL/ROI blank for every
// open position on Time Trading worker cards.
export interface FuturesMarkPosition {
  trade_id: string;
  symbol: string;
  side: "long" | "short";
  leverage: number;
  isolated_margin: number;
  notional: number;
  quantity: number;
  entry_price: number;
  entry_time: string;
  take_profit_price?: number | null;
  stop_loss_price?: number | null;
  liquidation_price?: number | null;
  mark_price: number;
  unrealized_gross_pnl: number;
  unrealized_net_pnl: number;
  margin_roi_pct: number;
}

export interface PaperSessionMark {
  timestamp: string;
  prices: Record<string, number>;
  position_values: Record<string, number>;
  position_pnl?: Record<string, number>;
  open_positions?: FuturesMarkPosition[];
  cash_remaining: number;
  reserved_margin?: number;
  open_notional?: number;
  wallet_balance?: number;
  available_balance?: number;
  unrealized_pnl?: number;
  funding_paid?: number;
  fees_paid?: number;
  equity: number;
  pnl: number;
  pnl_pct: number;
  leverage?: number;
  margin_mode?: string;
}

export interface PaperSessionTrade {
  timestamp: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  price: number;
  notional: number;
  fee_paid?: number;
  reason: string;
  realized_pnl?: number | null;
  gross_pnl?: number | null;
  entry_fee_allocated?: number | null;
  total_fees?: number | null;
  net_pnl?: number | null;
  entry_time?: string | null;
  entry_price?: number | null;
}

// FuturesPaperEngine's raw ClosedTrade row shape (trades.jsonl), returned
// verbatim as recent_trades for strategy_type "futures_paper_engine" --
// deliberately NOT the same shape as PaperSessionTrade above (that one is
// paper_session.py's equal-weight-rebalance BUY/SELL log). Rendering futures
// trades through the wrong shape is what silently emptied "Recent Closed
// Trades" for every control_*/candidate_* worker: PaperSessionTrade has no
// realized_pnl field, so the spot-only filter dropped every futures row.
export interface FuturesClosedTrade {
  trade_id: string;
  symbol: string;
  side: "long" | "short";
  margin_mode: string;
  leverage: number;
  margin_used: number;
  notional: number;
  quantity: number;
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  take_profit_price?: number | null;
  stop_loss_price?: number | null;
  liquidation_price?: number | null;
  gross_pnl: number;
  entry_fee: number;
  exit_fee: number;
  funding_paid: number;
  liquidation_fee: number;
  net_pnl: number;
  roi_pct: number;
  hold_seconds: number;
  entry_reason: string;
  exit_reason: string;
  market_regime: string;
}

export function isFuturesClosedTrade(tr: PaperSessionTrade | FuturesClosedTrade): tr is FuturesClosedTrade {
  return (tr as FuturesClosedTrade).exit_time !== undefined && (tr as FuturesClosedTrade).net_pnl !== undefined;
}

export interface PaperSessionLivePrices {
  prices: Record<string, number>;
  timestamp: string;
}

// Weighted-average-cost realized win/loss accounting, computed server-side
// from the trade log. null win_rate/avg_win/avg_loss/profit_factor means
// no closed (SELL) trades yet for that symbol -- not zero, unknown.
export interface PaperSessionTradeStatsRow {
  realized_pnl: number;
  win_count: number;
  loss_count: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  gross_profit_factor?: number | null;
  expectancy?: number | null;
  fees_paid: number;
  open_qty?: number;
  avg_cost?: number;
  entry_fee_basis?: number;
}

export interface PaperSessionTradeStats {
  overall: PaperSessionTradeStatsRow;
  by_symbol: Record<string, PaperSessionTradeStatsRow>;
}

// Derived status fields (see compute_session_status in agent/paper_session.py)
// -- deliberately explicit instead of inferring health from raw cash sign or
// assuming "directory exists" means "process is running".
export type PaperSessionRuntimeStatus = "running" | "stopped" | "unknown";
export type PaperSessionAnalysisStatus = "valid" | "reconstructed" | "tainted" | "invalid";
export type PaperSessionRole = "control" | "candidate" | "historical";

// Active/archived registry (config/paper_sessions_registry.json) split --
// separate from runtime_status above. "classification" says whether the
// registry knows this session at all; "status" is registry-aware and is
// the one safe to badge in the UI (an archived session can never say
// "running", even if a stale .heartbeat file is still sitting on disk).
export type PaperSessionClassification = "active" | "archived" | "unknown" | "historical";
export type PaperSessionRegistryStatus = "running" | "stale" | "archived" | "unknown";

export interface PaperSessionStatus {
  runtime_status: PaperSessionRuntimeStatus;
  analysis_status: PaperSessionAnalysisStatus;
  accounting_status: string;
  accounting_schema_version: number | null;
  session_role: PaperSessionRole;
  regimen: string;
  active: boolean;
  classification: PaperSessionClassification;
  status: PaperSessionRegistryStatus;
}

export type PaperSessionScope = "active" | "archived" | "all";

export interface PaperProviderHealthRow {
  provider: "okx" | "binance" | "bybit" | "gate";
  status: "ok" | "error";
  http_status: number | null;
  latency_ms: number;
  error: string | null;
}

export interface PaperProviderHealth {
  checked_at: string;
  priority: Array<PaperProviderHealthRow["provider"]>;
  providers: PaperProviderHealthRow[];
}

export interface PaperDecisionHealthWorker {
  worker_id: string;
  last_cycle_at: string | null;
  market_data_fresh: boolean;
  latest_rejections: { strategy: string | null; risk: string | null; order: string | null };
  latest_funnel: Record<string, unknown>;
  window: {
    cycles_completed: number;
    signals_evaluated: number;
    signals_true: number;
    entries_requested: number;
    paper_orders_filled: number;
    positions_closed: number;
  };
}

export interface PaperDecisionHealth {
  status: "ok" | "error";
  detail: string | null;
  window_hours: number;
  workers: PaperDecisionHealthWorker[];
}

export interface PaperSessionSummary extends PaperSessionStatus {
  session_id: string;
  session: PaperSessionConfig;
  book: PaperSessionBook | null;
  mark_count: number;
  latest_mark: PaperSessionMark | null;
  trade_count: number;
  recent_trades: (PaperSessionTrade | FuturesClosedTrade)[];
  trade_stats?: PaperSessionTradeStats;
  equity_curve: EquityPoint[];
  max_drawdown?: number;
  database_account?: PaperDatabaseAccount | null;
}

export interface PaperDatabaseAccount {
  account_id: string;
  strategy_id: string;
  worker_id: string;
  timeframe: string;
  mode: "paper" | "live";
  leverage: 5 | 10;
  initial_capital: number;
  cash_available: number;
  margin_used: number;
  open_positions: number;
  realized_pnl: number;
  unrealized_pnl: number;
  funding_pnl: number;
  fees: number;
  current_equity: number;
  last_heartbeat: string | null;
  last_trade: string | null;
  ledger_status: string;
  risk_state: Record<string, unknown>;
  /** Whichever exchange actually answered the most recent completed cycle
   * (okx/binance/bybit/gate) -- persisted by the worker, never inferred
   * client-side. null until at least one cycle has completed. */
  market_data_source?: "okx" | "binance" | "bybit" | "gate" | null;
  last_cycle_completed_at?: string | null;
}

export interface ShadowComparisonArm extends PaperSessionStatus {
  session_id: string;
  net_return: number | null;
  trade_count: number;
  rebalance_fees: number;
  total_fees: number;
  turnover: number;
  max_drawdown: number;
  tracking_error_rms: number | null;
  max_weight_drift: number;
  min_rebalance_notional?: number;
  reconciled: boolean;
}

export interface ShadowComparisonDelta {
  net_return: number;
  total_fees: number;
  trade_count: number;
  turnover: number;
  max_drawdown: number;
}

export interface ShadowComparisonRow {
  regimen: string;
  control_session_id: string | null;
  candidate_session_id: string | null;
  control: ShadowComparisonArm | null;
  candidate: ShadowComparisonArm | null;
  delta: ShadowComparisonDelta | null;
}

export interface MessageItem {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
  linked_attempt_id?: string;
  metadata?: Record<string, unknown>;
}
