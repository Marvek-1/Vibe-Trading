import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, ArrowDown, Radio } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const LIVE_POLL_INTERVAL_MS = 3_000;

type Direction = "up" | "down" | "flat";

interface TickerState {
  price: number;
  direction: Direction;
}

function fmtPrice(v: number): string {
  // Small-price symbols (e.g. DOGE, ADA) need more decimals to show movement at all.
  const decimals = v >= 100 ? 2 : v >= 1 ? 4 : 6;
  return v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/**
 * Fast (few-second) price-only poll, separate from the session's receipted
 * ledger. Never reads marks/trades/book -- purely a live display of where
 * prices are moving right now, between the ledger's own periodic marks.
 */
export function LiveTicker({ sessionId, symbols }: { sessionId: string; symbols: string[] }) {
  const [ticks, setTicks] = useState<Record<string, TickerState>>({});
  const [connected, setConnected] = useState(false);
  const prevRef = useRef<Record<string, number>>({});
  const mountedRef = useRef(false);

  const poll = useCallback(async () => {
    try {
      const { prices } = await api.getPaperSessionLivePrices(sessionId);
      if (!mountedRef.current) return;
      const next: Record<string, TickerState> = {};
      for (const sym of symbols) {
        const price = prices[sym];
        if (price == null) continue;
        const prev = prevRef.current[sym];
        const direction: Direction = prev == null || price === prev ? "flat" : price > prev ? "up" : "down";
        next[sym] = { price, direction };
        prevRef.current[sym] = price;
      }
      setTicks(next);
      setConnected(true);
    } catch {
      if (!mountedRef.current) return;
      setConnected(false);
    }
  }, [sessionId, symbols]);

  useEffect(() => {
    mountedRef.current = true;
    poll();
    const timer = window.setInterval(poll, LIVE_POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
    };
  }, [poll]);

  return (
    <div className="glass-panel rounded-xl border border-border/40 p-3">
      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground uppercase tracking-wide font-medium mb-2">
        <Radio className={cn("h-3 w-3", connected ? "text-success animate-pulse" : "text-muted-foreground")} />
        Live Prices {connected ? "" : "(reconnecting…)"}
      </div>
      <div className="flex flex-wrap gap-2">
        {symbols.map((sym) => {
          const t = ticks[sym];
          return (
            <div
              key={sym}
              className={cn(
                "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-mono tabular-nums transition-colors duration-500",
                t?.direction === "up" && "border-success/40 bg-success/10 text-success",
                t?.direction === "down" && "border-danger/40 bg-danger/10 text-danger",
                (!t || t.direction === "flat") && "border-border/60 text-foreground",
              )}
            >
              <span className="font-sans font-medium text-muted-foreground">{sym}</span>
              {t?.direction === "up" && <ArrowUp className="h-3 w-3" />}
              {t?.direction === "down" && <ArrowDown className="h-3 w-3" />}
              <span>{t ? fmtPrice(t.price) : "—"}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
