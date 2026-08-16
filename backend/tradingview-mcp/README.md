# tradingview_mcp 🜂

TradingView market intelligence + append-only trade journal, exposed as an MCP server.

**MoStar Industries / African Flame Initiative** — built by **The Flame Architect**.

## What it is

An MCP (Model Context Protocol) server that gives any MCP client (Claude Code, Claude Desktop, etc.) two capabilities:

1. **Live TradingView analysis** — recommendations, price snapshots, and core indicators (RSI, MACD, ADX, EMA20/50/200, ATR, Bollinger) via TradingView's public scanner endpoint (`tradingview-ta`). Single-interval or multi-timeframe confluence sweeps.
2. **An append-only trade journal** — four event types (`SIGNAL`, `ENTRY`, `EXIT`, `NOTE`) written to SQLite. **No update tool. No delete tool.** Corrections are appended as `NOTE` events referencing the original id. Every event carries `attested_by`.

Stats (`profit_factor`, win rate, resolved counts) are computed live from ENTRY↔EXIT pairs linked by `ref_id`. Unresolved entries contribute zero. Gate accounting starts at zero.

## Tools

| Tool | Kind | Purpose |
|---|---|---|
| `tv_get_analysis` | read | One symbol, one interval — full TA snapshot |
| `tv_multi_timeframe` | read | Confluence sweep across up to 6 timeframes |
| `journal_append` | write (append-only) | Seal a SIGNAL / ENTRY / EXIT / NOTE event |
| `journal_list` | read | Newest-first event listing with filters |
| `journal_stats` | read | PF, win rate, gross P/L from resolved pairs |
| `watchlist_add` | write (idempotent) | Track a symbol |
| `watchlist_scan` | read | Live 1h sweep of the whole watchlist |

## Install

```bash
pip install -r requirements.txt
```

## Run / attach

Stdio transport. Claude Code:

```bash
claude mcp add tradingview -- python3 /path/to/tradingview-mcp/server.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "python3",
      "args": ["/path/to/tradingview-mcp/server.py"]
    }
  }
}
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `TV_MCP_DB` | `~/.tradingview_mcp/journal.db` | SQLite journal location |

## Doctrine

- The journal is a ledger, not a state table. One row per event, forever.
- `attested_by` is never implicit. Tag your witnesses.
- The `.gitignore` keeps `.env` files and `*.db` out of the repository. They never enter git.

🜂 Ikang burns the receipt into the record.
