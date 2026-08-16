import { describe, expect, it } from "vitest";
import type { PaperSessionSummary } from "@/lib/api";
import { marketTelemetry, normalizeMarketSource } from "../PaperTrading";

describe("paper trading market provenance", () => {
  it("accepts every configured futures provider", () => {
    expect(normalizeMarketSource("OKX")).toBe("okx");
    expect(normalizeMarketSource("binance")).toBe("binance");
    expect(normalizeMarketSource("Bybit")).toBe("bybit");
    expect(normalizeMarketSource("gate")).toBe("gate");
    expect(normalizeMarketSource("invented-exchange")).toBeNull();
  });

  it("reports persisted Bybit telemetry as healthy when fresh", () => {
    const observedAt = "2026-08-10T05:00:00.000Z";
    const session = {
      latest_mark: { timestamp: observedAt, market_data_source: "bybit", market_data_observed_at: observedAt },
      database_account: { timeframe: "5m" },
      session: {},
    } as unknown as PaperSessionSummary;

    expect(marketTelemetry(session, Date.parse(observedAt) + 30_000)).toMatchObject({
      source: "bybit",
      rawSource: "bybit",
      status: "OK",
      ageMs: 30_000,
    });
  });

  it("does not turn an unknown provider into a healthy badge", () => {
    const observedAt = "2026-08-10T05:00:00.000Z";
    const session = {
      latest_mark: { timestamp: observedAt, market_data_source: "mystery" },
      database_account: { timeframe: "5m" },
      session: {},
    } as unknown as PaperSessionSummary;

    expect(marketTelemetry(session, Date.parse(observedAt) + 1_000).status).toBe("INVALID");
  });
});
