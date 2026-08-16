from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True, slots=True)
class TimeframePolicy:
    timeframe: Literal["5m", "10m", "15m"]
    decision_interval_seconds: int
    maximum_position_age_seconds: int
    expiry_action: Literal["REDUCE_ONLY_CLOSE"] = "REDUCE_ONLY_CLOSE"

TIMEFRAME_POLICIES = {"5m": TimeframePolicy("5m",300,300), "10m": TimeframePolicy("10m",600,600), "15m": TimeframePolicy("15m",900,900)}

def policy_for(value: str) -> TimeframePolicy:
    if value not in TIMEFRAME_POLICIES: raise ValueError(f"unsupported timeframe: {value}")
    return TIMEFRAME_POLICIES[value]
