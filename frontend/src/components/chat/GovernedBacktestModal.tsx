import { useState } from "react";
import { X, ShieldCheck, Loader2 } from "lucide-react";

export interface GovernedBacktestParams {
  symbols: string[];
  start_date: string;
  end_date: string;
  interval: string;
  initial_cash: number;
}

interface GovernedBacktestModalProps {
  open: boolean;
  submitting: boolean;
  onClose: () => void;
  onSubmit: (params: GovernedBacktestParams) => void;
}

/**
 * Structured form for POST /backtest/governed — bypasses chat/ReAct entirely.
 * No LLM plans this run: symbols, dates, and interval go straight to the
 * deterministic backend route, which reports success only when run_card.json
 * exists with every required truth field.
 */
export function GovernedBacktestModal({ open, submitting, onClose, onSubmit }: GovernedBacktestModalProps) {
  const [symbolsText, setSymbolsText] = useState("BTC-USDT, ETH-USDT, SOL-USDT");
  const [startDate, setStartDate] = useState("2024-01-01");
  const [endDate, setEndDate] = useState("2024-03-31");
  const [interval, setInterval] = useState("1D");
  const [initialCash, setInitialCash] = useState("100000");
  const [validationError, setValidationError] = useState<string | null>(null);

  if (!open) return null;

  const handleSubmit = () => {
    const symbols = symbolsText
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (symbols.length === 0) {
      setValidationError("At least one symbol is required.");
      return;
    }
    const nonUsdt = symbols.filter((s) => !s.endsWith("USDT") && !s.endsWith("-USDT"));
    if (nonUsdt.length > 0) {
      setValidationError(`Governed crypto backtests require explicit USDT pairs: ${nonUsdt.join(", ")}`);
      return;
    }
    setValidationError(null);
    onSubmit({
      symbols,
      start_date: startDate,
      end_date: endDate,
      interval,
      initial_cash: Number(initialCash) || 100_000,
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="glass-surface glass-surface--accent-edge w-full max-w-md rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-primary" />
            <h2 className="text-base font-semibold">Governed Backtest</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground transition-colors"
            disabled={submitting}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <p className="text-xs text-muted-foreground mb-4">
          Deterministic execution, no chat model in the loop. Crypto data source is Binance-first.
          Reports success only when a run card with full provenance exists.
        </p>

        <div className="space-y-3">
          <div>
            <label className="text-xs font-medium text-muted-foreground">Symbols (USDT pairs, comma-separated)</label>
            <input
              type="text"
              value={symbolsText}
              onChange={(e) => setSymbolsText(e.target.value)}
              className="mt-1 w-full px-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
              disabled={submitting}
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs font-medium text-muted-foreground">Start date</label>
              <input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="mt-1 w-full px-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
                disabled={submitting}
              />
            </div>
            <div className="flex-1">
              <label className="text-xs font-medium text-muted-foreground">End date</label>
              <input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="mt-1 w-full px-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
                disabled={submitting}
              />
            </div>
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs font-medium text-muted-foreground">Interval</label>
              <select
                value={interval}
                onChange={(e) => setInterval(e.target.value)}
                className="mt-1 w-full px-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
                disabled={submitting}
              >
                <option value="1D">1D</option>
                <option value="4H">4H</option>
                <option value="1H">1H</option>
                <option value="30m">30m</option>
                <option value="15m">15m</option>
                <option value="5m">5m</option>
                <option value="1m">1m</option>
              </select>
            </div>
            <div className="flex-1">
              <label className="text-xs font-medium text-muted-foreground">Initial cash</label>
              <input
                type="number"
                value={initialCash}
                onChange={(e) => setInitialCash(e.target.value)}
                className="mt-1 w-full px-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
                disabled={submitting}
              />
            </div>
          </div>
        </div>

        {validationError && (
          <p className="mt-3 text-xs text-destructive">{validationError}</p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-2 rounded-lg border text-sm hover:bg-muted transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting}
            className="px-3 py-2 rounded-lg bg-primary text-primary-foreground text-sm hover:opacity-90 transition-opacity disabled:opacity-40 flex items-center gap-2"
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            {submitting ? "Running…" : "Run Backtest"}
          </button>
        </div>
      </div>
    </div>
  );
}
