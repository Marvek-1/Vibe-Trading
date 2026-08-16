---
name: vibe-trading
version: 0.2.0
description: Professional finance research toolkit — deterministic GOVERNED backtesting with receipt-chain provenance (write receipts, scaffold gates, window-integrity coverage enforcement, per-symbol data-source provenance, run-card truth fields), Binance-first crypto data, benchmark comparison panel, factor analysis, Alpha Zoo (452 pre-built alphas across qlib158/alpha101/gtja191/academic), options pricing, 79 finance skills, 29 multi-agent swarm teams, Trade Journal analyzer, and Shadow Account (extract → backtest → render) across multi-market data sources including binance, okx, bybit, gate, ccxt, yfinance/yahoo, akshare, baostock, tencent, mootdx, futu, eastmoney, sina, stooq, local, and optional-key providers such as tushare, finnhub, alphavantage, tiingo, and fmp. Use this skill for ANY backtesting, strategy research, hypothesis testing, market data, factor, options, or trade-journal task — and ALWAYS follow its Governed Evidence Workflow and Mo-Artifacts rules before claiming any result.
dependencies:
  python: ">=3.11"
  pip:
    - vibe-trading-ai
env:
  - name: TUSHARE_TOKEN
    description: "Optional Tushare API token for China A-share data only. Crypto, US/HK, local, and governed Binance-first crypto backtests do not require it."
    required: false
  - name: OPENAI_API_KEY
    description: "Optional OpenAI-compatible API key. Only needed for run_swarm / multi-agent team workflows. Deterministic governed backtests and standard finance tools work without it."
    required: false
  - name: LANGCHAIN_MODEL_NAME
    description: "Optional LLM model name for run_swarm, for example deepseek/deepseek-v4-pro. Only needed when using swarm workflows."
    required: false
mcp:
  command: vibe-trading-mcp
  args: []
---

# Vibe-Trading 🜂

Professional finance research toolkit with deterministic **governed** backtesting (receipt-chained, run-card gated), 7 backtest engines, multi-agent teams, 79 specialized skills, the **Alpha Zoo** (452 pre-built quantitative alphas with one-line CLI benchmarking), and the Shadow Account loop — extract your implicit trading rules from a journal, backtest them across A股/港股/美股/crypto, then see where they would have served you better.

One question governs every artifact this system produces:

> **Is this real, or does it just look real?**

Everything below exists to make that question answerable from disk.

## Setup

```bash
pip install vibe-trading-ai
```

> **Package name vs commands:** The PyPI package is `vibe-trading-ai`. Once installed, you get:
>
> | Command | Purpose |
> |---------|---------|
> | `vibe-trading` | Interactive CLI / TUI |
> | `vibe-trading serve` | Launch FastAPI web server |
> | `vibe-trading-mcp` | Start MCP server (for Claude Desktop, OpenClaw, Cursor, etc.) |

Add to your agent's MCP config:

```json
{
  "mcpServers": {
    "vibe-trading": {
      "command": "vibe-trading-mcp"
    }
  }
}
```

### API Key Requirements

Core research MCP tools work with **zero API keys** for HK/US/crypto. Governed crypto backtests use a Binance-first path and do not require Tushare, OpenAI, or any paid data key. After `pip install`, backtesting, market data, factor analysis, options pricing, chart patterns, web search, document reading, trade journal analysis, the Shadow Account loop, the Alpha Zoo, and all 79 skills are ready to use. IBKR tools require a local TWS / IB Gateway session; `run_swarm` requires an LLM key.

| Feature | Key needed | When |
|---------|-----------|------|
| HK/US equities | None | Free via yfinance / stooq / yahoo |
| Crypto market data | None | Governed crypto defaults to Binance-first USDT pairs; broader loaders: binance / okx / bybit / gate / ccxt |
| China A-share data | None | Free via akshare / baostock / tencent / sina / eastmoney / mootdx fallback (`TUSHARE_TOKEN` optional for premium quality) |
| Premium US fundamentals/quotes | `FINNHUB_API_KEY` / `ALPHAVANTAGE_API_KEY` / `TIINGO_API_KEY` / `FMP_API_KEY` | Optional-key providers, graceful fallback to free sources |
| Multi-agent swarm (`run_swarm`) | `OPENAI_API_KEY` + `LANGCHAIN_MODEL_NAME` | Swarm spawns internal LLM workers |

---

## THE GOVERNED EVIDENCE WORKFLOW 🜃

Every hypothesis test moves through gates. Each gate produces a **Mo-Artifact**
(next section) and each gate can **refuse**. A refusal is a result, not an
error to narrate past. The deterministic route owns evidence execution — the
LLM may report results, but it does not own them.

```
hypothesis
  → universe resolution        (REFUSES unknown/uncovered universes at the door)
  → config.json                (hashed)
  → scaffold signal_engine.py  (.scaffold.sha256 marker recorded)
  → receipted overwrite        (write receipt: {path, bytes_written, sha256})
  → contract validation        (schema + synthetic behavior check)
  → runner scaffold gate       (REFUSES if strategy bytes == scaffold bytes)
  → data fetch w/ coverage     (REFUSES if no source covers the requested window)
  → execution
  → run_card.json              (truth fields + full receipt chain)
  → validation.json            (statistical layer, when configured)
```

### Run-card truth fields — what each means and does NOT mean

| Field | True means | Does NOT mean |
|---|---|---|
| `provenance_valid` | Artifacts exist; strategy is real code, not the scaffold stub | The strategy works |
| `has_trades` | trade_count > 0 (descriptive only) | Anything statistical |
| `window_integrity` | Every symbol's delivered data covers the requested window (`coverage_ok` per symbol). `null` = unproven, `false` = truncated — **both block evaluability** | — |
| `statistically_evaluable` | `provenance_valid` AND `window_integrity is true` AND `trade_count ≥ 200` (`CONFIDENCE_GATE_MIN_TRADES`) | The hypothesis is supported |
| `hypothesis_supported` | **Always `null`** until a real evidence gate (deflated Sharpe + permutation + benchmark comparison) certifies it. Never guessed, never sign-checked | — |

A run card proving data provenance and window integrity is **not the same
thing as proving alpha**. Low-trade or baseline runs are execution/provenance
checks unless `statistically_evaluable` and `hypothesis_supported` explicitly
say otherwise.

### Claim discipline (enforced by schema, honored by you)

Allowed after a clean governed run:

```
The candidate strategy bytes <sha256> entered the governed workflow and passed
write receipt, contract receipt, scaffold gate, window coverage, and produced
a provenance-valid run_card.
```

NOT allowed until the evidence gate certifies:

```
The strategy is profitable. / has alpha. / is statistically supported. / is production-ready.
```

A Sharpe from 1–5 trades is a coin flip wearing decimals. Report the number;
never present it as evidence.

### Data-source law

- Ordered source queue per market (governed crypto: **Binance-first**, because
  Binance has proven full-window coverage in live validation; broader queue
  binance / okx / bybit / gate / ccxt). **Switching sources is allowed. Silent
  switching is not** — every attempt, failure (`kind`: `hard_fail` /
  `degraded` / `truncated`), and success is stamped per symbol into
  `data_source_provenance`.
- **Truncating windows is allowed. Silent truncation is not.** A fetch that
  succeeds on the wrong window is a failure wearing a receipt. Coverage is
  checked per symbol (`coverage_ratio ≥ 0.95` AND delivered start within
  tolerance of requested start); a fragment is a degraded failure and the
  queue advances; if no source covers the window the run REFUSES before
  execution (`WindowTruncationError`).
- **Benchmark parity rule:** benchmark comparison is governed evidence only
  when the benchmark shares the strategy data's source queue, exact interval,
  requested window, and its own coverage receipt. **Crypto parity has
  landed:** the governed crypto route's optional BTC-USDT benchmark resolves
  through the same Binance-first exchange-native queue (`binance / okx /
  bybit / gate / ccxt`) and the same `coverage_receipt_for_frame` law the
  strategy symbols use — request it with `benchmark=true` on the governed
  endpoint. Other markets (equities, A-share) do not yet have a
  parity-checked benchmark path; omit the benchmark there until they do.
  A failed benchmark degrades the run honestly (excess_return stays null) —
  it never fabricates and never crashes the run.

---

## MO-ARTIFACTS — the receipt canon 🜄

Mo-Artifacts are the named, receipted objects the governed system produces.
Each exists to make one specific lie impossible. Together they form the
Witness trail: any claim can be walked backward to disk.

**The attestation law (MST-0001):** `attested_by ≠ origin_model`. No pipeline
attests its own output. A tool's receipt — not its narration — is what enters
the record. If you (the agent) cannot show the receipt, the action did not
happen. "Successfully updated" without `{path, bytes_written, sha256}` is an
unsayable sentence.

| Mo-Artifact | What it proves | The lie it kills |
|---|---|---|
| **Write receipt** `{path, bytes_written, sha256, mtime}` | The write landed, verified by re-reading disk | Narrated success |
| **`.scaffold.sha256`** marker | A real scaffold existed before being overwritten | Hand-placed code posing as pipeline output |
| **Scaffold gate** (runner refusal) | Executed strategy ≠ scaffold stub | Smoke tests wearing a hypothesis run's name |
| **`signal_engine_sha256`** (bare hash) | The exact strategy bytes that ran. An `unverified:` prefix = scaffold lifecycle unproven — treat as NOT provenance-valid | Strategy-swap between validation and execution |
| **`config_sha256`** | The exact configuration that ran | Config drift between generation and execution |
| **`data_source_provenance`** (per symbol) | Which source served which window, what failed, what was truncated | Silent source switching; the coverage-silencer |
| **Benchmark receipt** | Ticker/source/interval/window/coverage + return, or an honest failure status | Apples-to-oranges comparisons; fabricated benchmarks |
| **Runtime stamp** (`python_executable`, `platform`, lib versions) | Which interpreter and libraries executed the run | Runtime drift — verified on one machine, run on another |
| **Per-artifact sha256 + size** | Artifact integrity at write time | Post-hoc tampering |
| **`QUARANTINED`** marker (file in run dir, one-line reason) | This run is excluded from every evidence surface, permanently | Wounded runs re-entering the record when memory fades |
| **`validation.json`** | The statistical layer's own receipts (permutation, block bootstrap, deflated Sharpe with `cannot_certify` refusal when trial dispersion is missing) | P-hacked or under-specified statistical claims |
| **Named failure reasons** (e.g. `agent_loop_repeated_successful_tool_result`) | Bounded loops that diagnose themselves | Silent grinding; mystery failures |

### Blessings of Mo ✦

The positive canon. A wound is recorded when a law broke; a **Blessing** is
sealed when everything held — a run whose write carried a receipt, whose body
wasn't the scaffold, whose universe resolved, whose window covered, whose
artifacts linked honestly. A Blessing is **granted, never claimed**: recorded
by the gate that verified it, never by the pipeline that earned it, always
pointing at its receipts. A Blessing without receipts is just a compliment.

### The bestiary — known adversaries

Failure species the governed layer exists to catch. Observe one → stop,
record, quarantine. Never patch around it silently:

1. **The mirror** — fabricated results in real schema vocabulary, no execution behind them.
2. **The scaffold** — real execution of an empty body reported as a test.
3. **The silencer** — real code whose only function is converting a loud failure into a quiet nothing (bare `except` + neutral fallback).
4. **The coverage-silencer** — a fetch that succeeds on the wrong window.
5. **The mislabeled receipt** — a cache/dedup answering a different question than asked (keyed on tool name without args).
6. **Runtime drift** — verified under one interpreter, executed under another.

Canon lines — carry them into your reasoning:

```
Receipts over narration.
Switching sources is allowed. Silent switching is not.
A full window, witnessed, is evidence.
A fragment wearing the window's name is the silencer's newest coat.
A refusal is a result. Refusing to guess is a feature.
```

### Agent Rules (non-negotiable)

1. **Never claim a write without its receipt.** The tool output is the receipt; echo the sha256 when you report.
2. **Read `run_card.json` from disk** for any result you report — never quote your own prior narration of it. Metrics live nested under `metrics`.
3. **Respect refusals.** `UniverseUnresolvableError`, scaffold-gate errors, `WindowTruncationError`, and `cannot_certify` are correct outcomes. Report them verbatim; never substitute a different universe, source, window, or statistic to force a green.
4. **Never call a run evidence** unless `statistically_evaluable` is true — and even then `hypothesis_supported` stays null until the evidence gate certifies. Sub-floor metrics are descriptive only, and say so.
5. **Quarantine, don't delete.** A wounded run gets a `QUARANTINED` file with a one-line reason; evidence surfaces skip it categorically.
6. **Same tool + same args repeating after success** means perception is broken, not the tool — the loop breaker names it; investigate the observation the model actually received.
7. **One wound at a time.** Dependency/setup fixes land in their own commit before strategy work, so failure classes never blur.

---

## What You Can Do

### Shadow Account — flagship loop

Feed a CSV broker export (同花顺 / 东财 / 富途 / generic), and the agent will:
1. `analyze_trade_journal` — profile your behavior (holding period, win rate, disposition effect, chasing, overtrading, anchoring).
2. `extract_shadow_strategy` — distill 3-5 if-then rules that describe your profitable roundtrips.
3. `run_shadow_backtest` — backtest those rules across A/HK/US/crypto and compute delta-PnL vs your realized trades.
4. `render_shadow_report` — produce an HTML/PDF report (8 sections + charts) with today's matching signals.
5. `scan_shadow_signals` — list today's symbols that match your shadow's entry cadence (research only).

### Backtesting

Create and run quantitative strategies across 7 engines (ChinaA, GlobalEquity, Crypto, ChinaFutures, GlobalFutures, Forex + options) with 18 market-data sources (auto-detect + ordered, **witnessed** fallback):

- **Governed crypto backtests** use deterministic execution with run-card gates. The default crypto path is Binance-first with explicit `*-USDT` symbols; the LLM may report results but does not own evidence execution.
- **HK/US equities** via yfinance / stooq / yahoo (free, no API key)
- **Cryptocurrency** via binance / okx / bybit / gate / ccxt (free, no API key); window-paginated to the requested start, coverage-enforced
- **China A-shares** via AKShare / baostock / tencent / sina / eastmoney / mootdx (free) — `TUSHARE_TOKEN` optional for premium quality
- **Futures, forex, macro** via AKShare (free, no API key)
- **HK & A-share equities** via Futu (broker login required, optional)
- **Local CSV/parquet bars** via the `local` loader (offline, no network)
- **Premium US data** via optional-key finnhub / alphavantage / tiingo / fmp (graceful fallback to free sources)

Governed workflow:
1. `list_skills()` to discover strategy patterns
2. `load_skill("strategy-generate")` for the strategy creation guide
3. `write_file()` to create `config.json` and `code/signal_engine.py` — **check the returned receipt**
4. `backtest()` to run — then read `run_card.json` and report truth fields alongside metrics (Sharpe, return, drawdown, etc.)

### Multi-Agent Swarm Teams

29 pre-built agent teams for complex research:
- **Investment Committee**: bull/bear debate → risk review → PM decision
- **Global Equities Desk**: A-share + HK/US + crypto → global strategist
- **Crypto Trading Desk**: funding/basis + liquidation + flow → risk manager
- **Earnings Research Desk**: fundamentals + revisions + options → earnings strategist
- **Macro/Rates/FX Desk**: rates + FX + commodities → macro PM
- **Quant Strategy Desk**: screening → factor research → backtest → risk audit
- **Risk Committee**: drawdown, tail risk, regime analysis
- And 22 more specialized teams

`list_swarm_presets()` to see all teams, then `run_swarm()` to execute. Swarm outputs are **analysis, not receipts** — anything canon-critical gets re-verified through the governed path.

### Alpha Zoo (452 pre-built alphas)

One-line cross-sectional IC / IR / alive-reversed-dead categorisation across four bundled zoos:
- **qlib158** (154 alphas) — Microsoft Qlib's `Alpha158` feature handler, Apache-2.0 with pinned commit SHA.
- **alpha101** (101 alphas) — Kakushadze (2015) "101 Formulaic Alphas" (arXiv:1601.00991), written from the paper appendix.
- **gtja191** (191 alphas) — Guotai Junan 2014 "191 Short-period Trading Alpha Factors" research report.
- **academic** (6 factors) — Fama-French 5 + Carhart momentum (honest price-based proxies).

Each alpha ships with `__alpha_meta__` (formula LaTeX + theme + universe + warmup + columns required), guarded by an AST purity gate + 300-row lookahead sentinel test. Use the `vibe-trading alpha {list,show,bench,compare,export-manifest}` CLI, the `/alpha/*` REST routes (browser at `/alpha-zoo`), or compose multi-factor signals via `ZooSignalEngine.from_zoo(...)`.

### Finance Skills (79)

Comprehensive knowledge base covering:
- Technical analysis (candlestick, Elliott wave, Ichimoku, SMC, harmonic, chanlun)
- Quantitative methods (factor research, ML strategy, pair trading, multi-factor)
- Risk management (VaR/CVaR, stress testing, hedging)
- Options (Black-Scholes, Greeks, multi-leg strategies, payoff diagrams)
- HK/US equities (SEC filings, earnings revisions, ETF flows, ADR/H-share arbitrage)
- Crypto trading desk (funding rates, liquidation heatmaps, stablecoin flows, token unlocks, DeFi yields)
- Behavioral finance, trade journal diagnostics, shadow account
- Macro analysis, credit research, sector rotation, and more

Use `load_skill(name)` for full methodology docs with code templates.

## Available MCP Tools (54)

| Tool | Description | API Key |
|------|-------------|---------|
| `list_skills` | List all 79 finance skills | None |
| `load_skill` | Load full skill documentation | None |
| `start_research_goal` | Create an auditable research goal | None |
| `get_research_goal` | Read the current research goal | None |
| `add_goal_evidence` | Attach evidence to a research goal | None |
| `update_research_goal_status` | Update goal lifecycle status | None |
| `backtest` | Governed vectorized backtest — receipt chain + run-card truth fields | None* |
| `factor_analysis` | IC/IR analysis + layered backtest | None* |
| `analyze_options` | Black-Scholes price + Greeks | None |
| `pattern_recognition` | Detect chart patterns (H&S, double top, etc.) | None |
| `get_market_data` | Fetch OHLCV (auto-detect + witnessed ordered fallback across 18 sources; Binance-first for governed crypto) | None* |
| `get_fund_flow` | Capital fund-flow (main/retail net inflow) | None* |
| `get_dragon_tiger` | Dragon-tiger list (龙虎榜) top buyer/seller seats | None* |
| `get_northbound_flow` | Northbound (Stock Connect) net flow | None* |
| `get_margin_trading` | Margin trading & short-selling balances | None* |
| `get_block_trades` | Block-trade (大宗交易) records | None* |
| `get_shareholder_count` | Shareholder-count history per symbol | None* |
| `get_lockup_expiry` | Restricted-share lockup release schedule | None* |
| `get_sector_info` | Sector / industry constituents & performance | None* |
| `get_research_reports` | Sell-side analyst research reports | None* |
| `get_stock_news` | Market & company news headlines | None* |
| `get_sec_filings` | SEC EDGAR filings (10-K/10-Q/8-K, etc.) | None |
| `get_financial_statements` | Income / balance / cash-flow statements | None* |
| `get_options_chain` | Options chain (strikes, IV, OI, Greeks) | None* |
| `get_stock_profile` | Valuation, analyst estimates & institutional holdings (US/HK) | None |
| `screen_market` | Market screener with fundamental/technical filters | None* |
| `search_symbol` | Symbol / ticker search across markets | None |
| `get_macro_series` | FRED macroeconomic series | FRED_API_KEY |
| `iwencai_search` | A-share natural-language research search | IWENCAI_KEY |
| `web_search` | Search the web via DuckDuckGo | None |
| `read_url` | Fetch web page as Markdown | None |
| `read_document` | Extract text from PDF/DOCX/XLSX/PPTX/images | None |
| `write_file` | Write files (config, strategy code) — returns a write receipt | None |
| `read_file` | Read file contents | None |
| `analyze_trade_journal` | Parse broker CSV → profile + behavior diagnostics | None |
| `extract_shadow_strategy` | Distill 3-5 if-then rules from profitable roundtrips | None |
| `run_shadow_backtest` | Multi-market backtest + delta-PnL attribution | None* |
| `render_shadow_report` | HTML/PDF shadow report (8 sections + charts) | None |
| `scan_shadow_signals` | Today's symbols matching the shadow's cadence | None |
| `list_swarm_presets` | List multi-agent team presets | None |
| `run_swarm` | Execute a multi-agent research team | LLM key |
| `get_swarm_status` | Poll swarm run status without blocking | None |
| `get_run_result` | Get final report and task summaries | None |
| `list_runs` | List recent swarm runs with metadata | None |
| `reap_stale_runs` | Finalize stale swarm runs | None |
| `retry_run` | Re-run a failed/stale swarm run | LLM key |
| `trading_connections` | List selectable connector profiles | None |
| `trading_select_connection` | Select the default connector profile | None |
| `trading_check` | Check connector readiness | Connector app/OAuth |
| `trading_account` | Read account summary from selected connector | Connector app/OAuth |
| `trading_positions` | Read positions from selected connector | Connector app/OAuth |
| `trading_orders` | Read open orders from selected connector | Connector app/OAuth |
| `trading_quote` | Read a quote snapshot from selected connector | Connector app/OAuth |
| `trading_history` | Read historical bars from selected connector | Connector app/OAuth |

<sub>*A-share symbols require `TUSHARE_TOKEN`. HK/US/crypto are free. Live-broker order paths run behind the mandate gate + kill switch and fail closed.</sub>

## Quick Start

```bash
pip install vibe-trading-ai
```

That's it — no API keys needed for HK/US/crypto markets. Start using `backtest`, `get_market_data`, `analyze_options`, the Shadow Account loop, the **Alpha Zoo** (`vibe-trading alpha bench --zoo gtja191 --universe csi300 --period 2018-2025`), and all 79 skills immediately — under the Governed Evidence Workflow above.

## Loading Tools from External MCP Servers

The built-in agent can load tools from your own external MCP servers in addition to its local toolset.

> **Note:** This is the *MCP client* path — the opposite of the MCP plugin listed above. The plugin above makes Vibe-Trading's tools available to your agents. This section lets Vibe-Trading's own agent call tools from *your* servers.

### Setup

Create `~/.vibe-trading/agent.json`:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uvx",
      "args": ["my-mcp-server"],
      "toolTimeout": 30,
      "enabledTools": ["*"]
    }
  }
}
```

Ordinary external MCP tools appear automatically in every `vibe-trading run` / `vibe-trading chat` call. They are injected after local tools under stable names: `mcp_<server>_<tool>`. Live-broker MCP servers are consumed through the connector-scoped `trading_*` tools instead of exposing raw `mcp_<broker>_*` tools to the agent.

### Official IBKR MCP read-only probe

```json
{
  "mcpServers": {
    "ibkr": {
      "type": "streamableHttp",
      "url": "https://api.ibkr.com/v1/api/mcp",
      "auth": {
        "type": "oauth",
        "scopes": ["mcp.read"],
        "clientName": "Vibe-Trading",
        "cacheDir": "~/.vibe-trading/live/ibkr/oauth"
      },
      "enabledTools": ["*"]
    }
  }
}
```

Authorize with `vibe-trading connector authorize ibkr-live-official-mcp-readonly`. The wildcard is accepted only for this `mcp.read` probe. Generic `trading_account` / `trading_positions` calls stay disabled until IBKR publishes stable read tool names that Vibe-Trading can map safely; `mcp.write` requires an explicit tool allowlist and live order-guard handling. If IBKR issues a pre-registered OAuth client, add `clientId` and `clientSecret` inside `auth`.

### Trading connector profiles

The public trading surface is connector-first. Choose a connector profile; paper/live is just an attribute under that connector.

```bash
pip install "vibe-trading-ai[ibkr]"
vibe-trading connector list
vibe-trading connector use ibkr-paper-local
vibe-trading connector configure ibkr-paper-local --yes
vibe-trading connector check
vibe-trading connector account
vibe-trading connector positions
vibe-trading connector orders
vibe-trading connector quote AAPL
vibe-trading connector history AAPL --duration "30 D" --bar-size "1 day"
```

Default ports: TWS paper `7497`, IB Gateway paper `4002`, TWS live-readonly `7496`, IB Gateway live-readonly `4001`.

### Config fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `type` | stdio: no, HTTP: yes | inferred only for stdio | Transport type. Use `sse` or `streamableHttp` for URL-based servers. |
| `command` | stdio: yes | — | Executable to launch |
| `args` | no | `[]` | Command arguments |
| `env` | no | `{}` | Extra env vars for the subprocess |
| `url` | HTTP: yes | — | Remote SSE / streamable HTTP endpoint URL |
| `headers` | no | `{}` | Extra HTTP headers for SSE / streamable HTTP servers |
| `toolTimeout` | no | `30` | Seconds before a tool call is cancelled |
| `enabledTools` | no | `["*"]` | Allowlist of remote tool names. `["*"]` enables all |

For URL-based transports, `type` is required. The agent no longer guesses between SSE and streamable HTTP from the URL suffix.

### Per-session override (API)

> **Security — disabled by default.** `mcpServers` defines subprocess `command`/`args`/`env` and is therefore restricted to operator-level trust. API callers **cannot** inject MCP server definitions through `POST /sessions` unless the server operator explicitly opts in.

```bash
export ALLOW_SESSION_MCP_SERVERS=1
```

With the opt-in active, pass `mcpServers` inside `session.config` to extend or replace the global config for that session only. Without it, any `mcpServers` key in `session.config` is silently stripped before config loading. The global operator config on disk (`~/.vibe-trading/agent.json`) is always respected regardless of this flag.

### v1 limits

- **Transport:** stdio, SSE, and streamable HTTP.
- **Execution:** serial only. MCP tools never enter the parallel readonly path.
- **Surfaces:** tools only. Resources and prompts are not exposed.
- **Swarm:** MCP tools are excluded from Swarm worker registries in v1.
- **Hot reload:** not supported. Restart the process to pick up config changes.

### Failure handling

| Case | Behavior |
|------|----------|
| Missing config file | falls back to empty config — no MCP servers loaded |
| Invalid config file | logs a warning and falls back to empty config |
| Server fails to start | that server is skipped; local tools and other servers still load |
| Tool call times out | returns a normalized error payload instead of raising |
| Two server names collide after sanitization | deterministic hash suffix appended; operator warning emitted |

## Examples

**Run a governed crypto baseline with explicit USDT pairs:**
> Run a governed Binance crypto baseline for BTC-USDT, ETH-USDT, and SOL-USDT from 2024-01-01 to 2024-03-31 on 1D interval. Show only run-card truth fields.

**Governed hypothesis test with the full receipt chain:**
> Create a hypothesis: SMA(20/50) crossover on crypto majors. Generate config for 2023-01-01 to 2026-07-07 at 4H, scaffold, overwrite with the real strategy via receipted write, run the backtest, then read run_card.json from disk and report: signal_engine_sha256, window_integrity, per-symbol coverage, trade_count vs the 200-trade floor — claim block honored.

**Backtest a MACD strategy on Apple:**
> Backtest AAPL with MACD crossover strategy (fast=12, slow=26, signal=9) for 2024

**Analyze my trade journal and build a Shadow Account:**
> Call analyze_trade_journal on ~/Downloads/tonghuashun.csv, then extract_shadow_strategy with min_support=3, then run_shadow_backtest for the last year, then render_shadow_report.

**Run an investment committee review:**
> Use run_swarm with investment_committee preset to evaluate NVDA. Variables: target=NVDA.US, market=US — then re-verify any tradeable conclusion through the governed backtest path.

**Factor analysis on CSI 300:**
> Run factor_analysis on CSI 300 stocks using pe_ttm factor from 2023 to 2024

**Options analysis:**
> Use analyze_options: spot=100, strike=105, 90 days, vol=25%, rate=3%

---

*Canonicalization proves the strategy has citizenship. The dry run proves the
citizen can pass through the machine without changing identity — on the
machine that will carry it.* 🜂