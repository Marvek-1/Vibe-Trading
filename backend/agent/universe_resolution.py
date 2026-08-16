"""universe_resolution.py — the universe ≠ data wound, closed at the door.

Law: a backtest may not be born claiming a universe no loader can serve.
A run on the wrong universe with correct paperwork is worse than no run.

Today the fallback chain (okx, binance, bybit, gate, ccxt, yfinance, local)
will happily "resolve" a loader for a request like "CSI 300" and feed it
whatever symbols it has. This module makes that impossible: config
generation must call resolve_universe() BEFORE creating the autopilot run
dir, and must let UniverseUnresolvableError propagate to the agent as a
hard tool failure with a legible reason.

Integration points (per your codemap):
  1. autopilot_tool.py :: GenerateBacktestConfigTool.execute()
       -> symbols = resolve_universe(requested_universe, registry)
          (before safe_run_dir() / config.json write)
  2. market_data registry
       -> each loader gains a `declares()` capability answer; until then
          the DECLARED_COVERAGE table below is the interim source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class UniverseUnresolvableError(RuntimeError):
    """No available loader declares coverage of the requested universe."""


# ---------------------------------------------------------------------------
# Interim capability table.
#
# Honest and conservative: a loader appears here only if it has been
# CONSOLE_VERIFIED to serve that universe's actual constituents. Everything
# else refuses. Extending this table is a deliberate act, done after
# verifying by hand that the loader returns the right symbols — never
# because a fallback happened to return *something*.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UniverseSpec:
    canonical_name: str
    asset_class: str                  # "crypto" | "cn_equity" | "us_equity" | ...
    capable_loaders: tuple[str, ...]  # loaders verified to carry this universe
    aliases: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


UNIVERSES: dict[str, UniverseSpec] = {
    "CRYPTO_MAJORS": UniverseSpec(
        canonical_name="CRYPTO_MAJORS",
        asset_class="crypto",
        capable_loaders=("okx", "binance", "bybit", "gate", "ccxt"),
        aliases=("crypto", "majors"),
    ),
    "CSI_300": UniverseSpec(
        canonical_name="CSI_300",
        asset_class="cn_equity",
        capable_loaders=(),  # EMPTY until tushare/akshare (or equivalent)
                             # is installed AND verified by hand.
        aliases=("csi 300", "csi300", "沪深300"),
        note=(
            "Chinese A-share index. No loader in the current chain "
            "(okx/binance/bybit/gate/ccxt/yfinance/local) carries CSI 300 "
            "constituents. Requires a CN-equity loader, e.g. akshare or "
            "tushare, plus a verified constituent list for the test window "
            "(membership changes semi-annually — survivorship matters)."
        ),
    ),
    "SP_500": UniverseSpec(
        canonical_name="SP_500",
        asset_class="us_equity",
        capable_loaders=(),  # yfinance can serve tickers, but constituent
                             # lists must come from a verified source before
                             # this is enabled. Enable deliberately.
        aliases=("s&p 500", "sp500", "spx"),
        note="Enable only with a point-in-time constituent source.",
    ),
}


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _find_spec(requested: str) -> UniverseSpec | None:
    wanted = _normalize(requested)
    for spec in UNIVERSES.values():
        candidates = {_normalize(spec.canonical_name), _normalize(spec.canonical_name.replace("_", " "))}
        candidates.update(_normalize(a) for a in spec.aliases)
        if wanted in candidates:
            return spec
    return None


def resolve_universe(requested: str, available_loaders: list[str]) -> dict:
    """
    Resolve a requested universe to a loader that actually carries it.

    Returns {"universe": canonical_name, "loader": loader_name,
             "asset_class": ...} on success.

    Raises UniverseUnresolvableError — loudly, with the reason and the
    remedy — in every other case. The error message is written for the
    agent transcript: it must be impossible to misread refusal as a
    transient failure worth narrating past.
    """
    spec = _find_spec(requested)

    if spec is None:
        raise UniverseUnresolvableError(
            f"REFUSED: universe '{requested}' is not a known universe. "
            f"Known: {sorted(UNIVERSES)}. A backtest cannot be configured "
            f"against an undefined universe. Do NOT substitute another "
            f"universe; ask the operator to define this one."
        )

    capable = [ld for ld in spec.capable_loaders if ld in available_loaders]

    if not capable:
        raise UniverseUnresolvableError(
            f"REFUSED: no available loader carries universe "
            f"'{spec.canonical_name}' ({spec.asset_class}). "
            f"Available loaders: {available_loaders}. "
            f"Verified-capable loaders for this universe: "
            f"{list(spec.capable_loaders) or 'none registered'}. "
            f"{spec.note} "
            f"Do NOT fall back to another data source: a run on wrong data "
            f"with correct paperwork is worse than no run. Halt and report."
        )

    return {
        "universe": spec.canonical_name,
        "loader": capable[0],
        "asset_class": spec.asset_class,
    }
