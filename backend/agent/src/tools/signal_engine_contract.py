"""Write-time SignalEngine contract validation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


class SignalEngineContractError(ValueError):
    """Raised when generated ``signal_engine.py`` violates the runtime contract."""


VALIDATOR_VERSION = "signal-engine-contract-v1"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonicalization_pass(proof: dict[str, Any]) -> bool:
    candidate_hash = proof.get("candidate_hash")
    governed_hash = proof.get("governed_hash")
    write_receipt_hash = proof.get("write_receipt_hash")
    contract_receipt_hash = proof.get("contract_receipt_hash")
    return (
        bool(candidate_hash)
        and candidate_hash == governed_hash == write_receipt_hash == contract_receipt_hash
    )


def validate_signal_engine_contract(
    source: str,
    *,
    signal_path: Path,
    run_dir: Path | None = None,
    candidate_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Execute ``SignalEngine.generate`` on synthetic data and verify shape.

    This proves more than byte persistence: the written source can be imported,
    instantiated, called with the run's configured symbols, and returns the
    shape the selected backtest engine expects.
    """
    if candidate_bytes is None:
        candidate_bytes = source.encode("utf-8")

    config = _load_config(signal_path=signal_path, run_dir=run_dir)
    engine = str(config.get("engine", "daily") or "daily")
    codes = _configured_codes(config)
    bars = max(90, int(config.get("contract_validation_bars", 0) or 0))
    signal_engine_sha256 = sha256_bytes(candidate_bytes)
    config_sha256 = _config_sha256(signal_path=signal_path, run_dir=run_dir)

    engine_cls = _load_signal_engine_class(source)
    instance = engine_cls()
    data_map = _synthetic_data_map(codes, bars=bars)

    try:
        output = instance.generate(data_map)
    except Exception as exc:  # noqa: BLE001 - return actionable tool error
        raise SignalEngineContractError(
            f"SignalEngine.generate() raised {type(exc).__name__}: {exc}"
        ) from exc

    if engine == "options":
        return _validate_options_output(
            output,
            codes=codes,
            bars=bars,
            signal_engine_sha256=signal_engine_sha256,
            config_sha256=config_sha256,
        )
    return _validate_daily_output(
        output,
        data_map=data_map,
        codes=codes,
        bars=bars,
        signal_engine_sha256=signal_engine_sha256,
        config_sha256=config_sha256,
    )


def _load_config(*, signal_path: Path, run_dir: Path | None) -> dict[str, Any]:
    root = run_dir
    if root is None and signal_path.parent.name == "code":
        root = signal_path.parent.parent

    if root is None:
        return {}

    config_path = root / "config.json"
    if not config_path.exists():
        return {}

    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SignalEngineContractError(f"config.json parse error: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SignalEngineContractError("config.json must contain a JSON object")
    return loaded


def _configured_codes(config: dict[str, Any]) -> list[str]:
    raw = config.get("codes")
    if isinstance(raw, list):
        codes = [str(item).strip() for item in raw if str(item).strip()]
        if codes:
            return codes
    return ["TEST.US"]


def _config_sha256(*, signal_path: Path, run_dir: Path | None) -> str | None:
    root = run_dir
    if root is None and signal_path.parent.name == "code":
        root = signal_path.parent.parent
    if root is None:
        return None
    config_path = root / "config.json"
    if not config_path.exists():
        return None
    return sha256_bytes(config_path.read_bytes())


def _load_signal_engine_class(source: str):
    with tempfile.TemporaryDirectory(prefix="signal_contract_") as tmp:
        path = Path(tmp) / "signal_engine.py"
        path.write_text(source, encoding="utf-8")

        from backtest.runner import (
            _validate_signal_engine_class,
            _validate_signal_engine_source,
        )

        try:
            _validate_signal_engine_source(path)
        except ValueError as exc:
            raise SignalEngineContractError(str(exc)) from exc

        module_name = f"_signal_engine_contract_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise SignalEngineContractError("Could not load signal_engine.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)

        engine_cls = getattr(module, "SignalEngine", None)
        if engine_cls is None:
            raise SignalEngineContractError("SignalEngine class not found")
        try:
            _validate_signal_engine_class(engine_cls)
        except ValueError as exc:
            raise SignalEngineContractError(str(exc)) from exc
        return engine_cls


def _synthetic_data_map(codes: list[str], *, bars: int) -> dict[str, pd.DataFrame]:
    index = pd.date_range("2024-01-01", periods=bars, freq="D")
    base = np.linspace(100.0, 130.0, bars)
    wave = np.sin(np.linspace(0.0, 6.0, bars)) * 4.0
    close = base + wave
    open_ = close * 0.998
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volume = np.linspace(1_000_000.0, 1_500_000.0, bars)

    data: dict[str, pd.DataFrame] = {}
    for idx, code in enumerate(codes):
        offset = float(idx) * 3.0
        data[code] = pd.DataFrame(
            {
                "open": open_ + offset,
                "high": high + offset,
                "low": low + offset,
                "close": close + offset,
                "volume": volume + idx,
            },
            index=index,
        )
    return data


def _validate_daily_output(
    output: Any,
    *,
    data_map: dict[str, pd.DataFrame],
    codes: list[str],
    bars: int,
    signal_engine_sha256: str,
    config_sha256: str | None,
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise SignalEngineContractError(
            f"SignalEngine.generate() must return dict[str, pd.Series], got {type(output).__name__}"
        )

    output_keys = {str(key) for key in output}
    expected_keys = set(codes)
    missing = sorted(expected_keys - output_keys)
    extra = sorted(output_keys - expected_keys)
    if missing or extra:
        raise SignalEngineContractError(
            f"SignalEngine.generate() output keys must match config codes; missing={missing}, extra={extra}"
        )

    summary: dict[str, Any] = {
        "status": "ok",
        "contract_valid": True,
        "engine": "daily",
        "codes": codes,
        "bars": bars,
        "output_keys": sorted(output_keys),
        "signal_engine_sha256": signal_engine_sha256,
        "config_sha256": config_sha256,
        "validated_symbols": sorted(codes),
        "returned_symbols": sorted(output_keys),
        "synthetic_rows": bars,
        "validator_version": VALIDATOR_VERSION,
        "warnings": [],
    }

    for code in codes:
        series = output[code]
        if not isinstance(series, pd.Series):
            raise SignalEngineContractError(
                f"SignalEngine.generate()[{code!r}] must be pd.Series, got {type(series).__name__}"
            )
        if not series.index.equals(data_map[code].index):
            raise SignalEngineContractError(
                f"SignalEngine.generate()[{code!r}] index must match input frame index"
            )
        numeric = pd.to_numeric(series, errors="coerce")
        finite = numeric.dropna()
        if not np.isfinite(finite.to_numpy(dtype=float)).all():
            raise SignalEngineContractError(
                f"SignalEngine.generate()[{code!r}] contains non-finite signal values"
            )
        if not finite.empty and ((finite < -1.0) | (finite > 1.0)).any():
            raise SignalEngineContractError(
                f"SignalEngine.generate()[{code!r}] values must be between -1.0 and 1.0"
            )
    return summary


def _validate_options_output(
    output: Any,
    *,
    codes: list[str],
    bars: int,
    signal_engine_sha256: str,
    config_sha256: str | None,
) -> dict[str, Any]:
    if not isinstance(output, list):
        raise SignalEngineContractError(
            f"Options SignalEngine.generate() must return list[dict], got {type(output).__name__}"
        )
    invalid = [idx for idx, item in enumerate(output) if not isinstance(item, dict)]
    if invalid:
        raise SignalEngineContractError(
            f"Options SignalEngine.generate() returned non-dict instructions at indexes {invalid}"
        )
    return {
        "status": "ok",
        "contract_valid": True,
        "engine": "options",
        "codes": codes,
        "bars": bars,
        "instruction_count": len(output),
        "signal_engine_sha256": signal_engine_sha256,
        "config_sha256": config_sha256,
        "validated_symbols": sorted(codes),
        "returned_symbols": sorted(codes),
        "synthetic_rows": bars,
        "validator_version": VALIDATOR_VERSION,
        "warnings": [],
    }
