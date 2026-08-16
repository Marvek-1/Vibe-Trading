"""Quick benchmark of all candidate strategies on the full historical dataset."""

from __future__ import annotations

from profit_lab.run import main


def run():
    for strat in ["momentum", "trend", "breakout", "mean_rev"]:
        print("\n" + "=" * 60)
        print("=== " + strat.upper() + " ===")
        print("=" * 60)
        main([strat])


if __name__ == "__main__":
    run()
