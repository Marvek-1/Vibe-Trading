"""Credential-free health probes for the paper market-data providers."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

PROVIDER_PRIORITY = ("okx", "binance", "bybit", "gate")
_ENDPOINTS = {
    "okx": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
    "binance": "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT",
    "bybit": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
    "gate": "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT",
}
_CACHE_SECONDS = 15.0
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_value: dict[str, Any] | None = None


def _valid_payload(provider: str, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if provider == "okx":
        return payload.get("code") == "0" and bool(payload.get("data"))
    if provider == "binance":
        return bool(payload.get("symbol")) and bool(payload.get("price"))
    if provider == "bybit":
        return payload.get("retCode") == 0 and bool(payload.get("result", {}).get("list"))
    if provider == "gate":
        return bool(payload.get("name")) and payload.get("last_price") is not None
    return False


def _probe(provider: str) -> dict[str, Any]:
    started = time.monotonic()
    status_code: int | None = None
    try:
        request = urllib.request.Request(
            _ENDPOINTS[provider],
            headers={"Accept": "application/json", "User-Agent": "vibe-trading-provider-health/1"},
        )
        with urllib.request.urlopen(request, timeout=4.0) as response:
            status_code = response.status
            payload = json.loads(response.read(1_000_000).decode("utf-8"))
        if status_code != 200 or not _valid_payload(provider, payload):
            raise ValueError("invalid provider response")
        return {
            "provider": provider,
            "status": "ok",
            "http_status": status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": None,
        }
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {
            "provider": provider,
            "status": "error",
            "http_status": status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }


def get_market_provider_health() -> dict[str, Any]:
    """Probe all providers concurrently, caching the receipt for 15 seconds."""
    global _cache_at, _cache_value
    now = time.monotonic()
    with _cache_lock:
        if _cache_value is not None and now - _cache_at < _CACHE_SECONDS:
            return _cache_value

    with ThreadPoolExecutor(max_workers=len(PROVIDER_PRIORITY)) as pool:
        by_provider = dict(zip(PROVIDER_PRIORITY, pool.map(_probe, PROVIDER_PRIORITY)))
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "priority": list(PROVIDER_PRIORITY),
        "providers": [by_provider[name] for name in PROVIDER_PRIORITY],
    }
    with _cache_lock:
        _cache_at = time.monotonic()
        _cache_value = result
    return result
