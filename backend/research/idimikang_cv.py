"""Walk-forward cross-validation for IdimIkang resolved signals.

Reads a JSON list of resolved signals, splits them chronologically,
learns a score threshold on the training fold, and evaluates it on
the held-out fold.  Reports monotonic score buckets, correlation, and
true out-of-sample profit factor / expectancy.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "idimikang_signals_raw.json"


def _iso_to_epoch(ts: Any) -> float:
    return datetime.fromisoformat(str(ts)).timestamp()


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def load_resolved(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    signals = data.get("signals", data) if isinstance(data, dict) else data
    if not isinstance(signals, list):
        raise ValueError("expected a list of signals or {'signals': [...]}")
    cleaned: list[dict[str, Any]] = []
    for s in signals:
        if not isinstance(s, dict):
            continue
        r = _as_float(s.get("r_multiple"))
        score = _as_float(s.get("score"))
        if r is None or score is None:
            continue
        cleaned.append({
            "signal_id": s.get("signal_id"),
            "ts": s.get("ts"),
            "epoch": _iso_to_epoch(s.get("ts")) if s.get("ts") else 0.0,
            "pair": s.get("pair"),
            "side": s.get("side", "LONG"),
            "r": r,
            "score": score,
            "execution_score": _as_float(s.get("execution_score")),
            "setup_score": _as_float(s.get("setup_score")),
            "prob_score": _as_float(s.get("prob_score")),
            "pwin": _as_float(s.get("pwin")),
            "moedge_score": _as_float(s.get("moedge_score")),
            "z_score": _as_float(s.get("z_score")),
            "regime": s.get("regime"),
            "signal_family": s.get("signal_family"),
        })
    cleaned.sort(key=lambda x: x["epoch"])
    return cleaned


@dataclass
class Metrics:
    count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    profit_factor: float | None = None
    win_loss_count_ratio: float | None = None
    gross_positive_r: float = 0.0
    gross_negative_r: float = 0.0


def compute_metrics(rs: list[float]) -> Metrics:
    if not rs:
        return Metrics()
    positive = [r for r in rs if r > 0]
    negative = [r for r in rs if r < 0]
    wins = len(positive)
    losses = len(negative)
    gross_positive = sum(positive)
    gross_negative = abs(sum(negative))
    profit_factor = (gross_positive / gross_negative) if gross_negative > 0 else (float("inf") if gross_positive > 0 else None)
    wl_ratio = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else None)
    return Metrics(
        count=len(rs),
        wins=wins,
        losses=losses,
        win_rate=wins / len(rs) if rs else 0.0,
        expectancy=statistics.mean(rs) if rs else 0.0,
        profit_factor=profit_factor,
        win_loss_count_ratio=wl_ratio,
        gross_positive_r=gross_positive,
        gross_negative_r=gross_negative,
    )


def rank_correlation(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation using average ranks; returns 0 on perfect ties."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0

    def ranks(values: list[float]) -> list[float]:
        indexed = sorted(range(n), key=lambda i: values[i])
        result = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
                j += 1
            avg = (i + 1 + j + 1) / 2.0
            for k in range(i, j + 1):
                result[indexed[k]] = avg
            i = j + 1
        return result

    rx, ry = ranks(xs), ranks(ys)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    den_x = math.sqrt(sum((x - mean_rx) ** 2 for x in rx))
    den_y = math.sqrt(sum((y - mean_ry) ** 2 for y in ry))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def make_buckets(signals: list[dict[str, Any]], feature: str, n: int = 4) -> list[dict[str, Any]]:
    values = [(s[feature], s["r"]) for s in signals if s.get(feature) is not None]
    if not values:
        return []
    values.sort(key=lambda x: x[0])
    bucket_size = max(1, len(values) // n)
    buckets: list[list[float]] = []
    for i in range(0, len(values), bucket_size):
        chunk = values[i:i + bucket_size]
        buckets.append([r for _, r in chunk])
    # Trim to n buckets if extras.
    while len(buckets) > n:
        buckets[-2].extend(buckets[-1])
        buckets.pop()
    result = []
    for idx, rs in enumerate(buckets):
        result.append({
            "bucket": idx,
            "score_min": min(v for v, _ in values[sum(len(b) for b in buckets[:idx]):sum(len(b) for b in buckets[:idx + 1])]) or 0,
            "score_max": max(v for v, _ in values[sum(len(b) for b in buckets[:idx]):sum(len(b) for b in buckets[:idx + 1])]) or 0,
            "count": len(rs),
            "expectancy": statistics.mean(rs) if rs else 0.0,
            "win_rate": len([r for r in rs if r > 0]) / len(rs) if rs else 0.0,
            "profit_factor": compute_metrics(rs).profit_factor,
            "gross_positive_r": compute_metrics(rs).gross_positive_r,
            "gross_negative_r": compute_metrics(rs).gross_negative_r,
        })
    return result


def find_threshold(train: list[dict[str, Any]], feature: str, min_coverage: float = 0.1) -> tuple[float, dict[str, Any]]:
    """Return the threshold (>=) that maximizes training expectancy with coverage."""
    values = sorted({s[feature] for s in train if s.get(feature) is not None})
    best = {"threshold": -float("inf"), "metrics": compute_metrics([s["r"] for s in train])}
    for threshold in values:
        selected = [s for s in train if s.get(feature) is not None and s[feature] >= threshold]
        coverage = len(selected) / len(train)
        if coverage < min_coverage:
            continue
        metrics = compute_metrics([s["r"] for s in selected])
        # Prefer profit factor >= 1 and positive expectancy; else just best expectancy.
        current_best = best["metrics"]
        qualifies = metrics.profit_factor is not None and metrics.profit_factor >= 1.0 and metrics.expectancy > 0
        current_qualifies = current_best.profit_factor is not None and current_best.profit_factor >= 1.0 and current_best.expectancy > 0
        if qualifies and not current_qualifies:
            best = {"threshold": threshold, "metrics": metrics}
        elif qualifies == current_qualifies and metrics.expectancy > current_best.expectancy:
            best = {"threshold": threshold, "metrics": metrics}
    return best["threshold"], best["metrics"]


def walk_forward_cv(
    signals: list[dict[str, Any]],
    feature: str,
    n_splits: int = 4,
    n_buckets: int = 4,
    min_coverage: float = 0.1,
) -> dict[str, Any]:
    if len(signals) < n_splits * 2:
        raise ValueError(f"not enough signals ({len(signals)}) for {n_splits} splits")

    test_size = max(1, len(signals) // n_splits)
    splits: list[dict[str, Any]] = []
    fold_metrics: list[dict[str, Any]] = []
    bucket_aggregates: dict[int, list[float]] = {i: [] for i in range(n_buckets)}

    for i in range(1, n_splits + 1):
        test_start = (i - 1) * test_size
        test_end = min(i * test_size, len(signals))
        train = signals[:test_start] + signals[test_end:] if i < n_splits else signals[:test_start]
        test = signals[test_start:test_end]

        threshold, train_metrics = find_threshold(train, feature, min_coverage)
        test_selected = [s for s in test if s.get(feature) is not None and s[feature] >= threshold]
        test_metrics = compute_metrics([s["r"] for s in test_selected])

        # Monotonic bucket check on training data.
        train_buckets = make_buckets(train, feature, n=n_buckets)
        test_buckets = make_buckets(test, feature, n=n_buckets)
        bucket_expectancies_train = [b["expectancy"] for b in train_buckets]
        bucket_expectancies_test = [b["expectancy"] for b in test_buckets]
        train_monotonic = rank_correlation(list(range(len(bucket_expectancies_train))), bucket_expectancies_train)
        test_monotonic = rank_correlation(list(range(len(bucket_expectancies_test))), bucket_expectancies_test)

        # Full correlation.
        train_corr = rank_correlation([s[feature] for s in train if s.get(feature) is not None],
                                      [s["r"] for s in train if s.get(feature) is not None])
        test_corr = rank_correlation([s[feature] for s in test if s.get(feature) is not None],
                                     [s["r"] for s in test if s.get(feature) is not None])

        splits.append({
            "fold": i,
            "train_count": len(train),
            "test_count": len(test),
            "threshold": threshold,
            "train_coverage": len([s for s in train if s.get(feature) is not None and s[feature] >= threshold]) / len(train),
            "test_coverage": len(test_selected) / len(test) if test else 0.0,
            "train_metrics": _metrics_to_dict(train_metrics),
            "test_metrics": _metrics_to_dict(test_metrics),
            "train_bucket_monotonicity": train_monotonic,
            "test_bucket_monotonicity": test_monotonic,
            "train_score_correlation": train_corr,
            "test_score_correlation": test_corr,
        })
        fold_metrics.append({
            "fold": i,
            "test_selected": len(test_selected),
            "test_expectancy": test_metrics.expectancy,
            "test_profit_factor": test_metrics.profit_factor,
        })

    all_values = [s[feature] for s in signals if s.get(feature) is not None]
    full_threshold, _ = find_threshold(signals, feature, min_coverage)
    full_selected = [s for s in signals if s.get(feature) is not None and s[feature] >= full_threshold]
    full_metrics = compute_metrics([s["r"] for s in full_selected])
    all_metrics = compute_metrics([s["r"] for s in signals])

    return {
        "feature": feature,
        "signal_count": len(signals),
        "n_splits": n_splits,
        "selected_threshold": full_threshold,
        "out_of_sample_folds": splits,
        "baseline_all_signals": _metrics_to_dict(all_metrics),
        "selected_signals": _metrics_to_dict(full_metrics),
        "coverage": len(full_selected) / len(signals) if signals else 0.0,
        "avg_test_expectancy": statistics.mean(f["test_expectancy"] for f in fold_metrics) if fold_metrics else 0.0,
    }


def _metrics_to_dict(m: Metrics) -> dict[str, Any]:
    return {
        "count": m.count,
        "wins": m.wins,
        "losses": m.losses,
        "win_rate": m.win_rate,
        "expectancy": m.expectancy,
        "profit_factor": m.profit_factor,
        "win_loss_count_ratio": m.win_loss_count_ratio,
        "gross_positive_r": m.gross_positive_r,
        "gross_negative_r": m.gross_negative_r,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--features", default="score,execution_score,setup_score,moedge_score,pwin,prob_score,z_score")
    parser.add_argument("--n-splits", type=int, default=4)
    parser.add_argument("--n-buckets", type=int, default=4)
    parser.add_argument("--min-coverage", type=float, default=0.1)
    args = parser.parse_args()

    signals = load_resolved(args.input)
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    results = []
    for feature in features:
        try:
            results.append(walk_forward_cv(signals, feature, args.n_splits, args.n_buckets, args.min_coverage))
        except ValueError as exc:
            results.append({"feature": feature, "error": str(exc)})
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
