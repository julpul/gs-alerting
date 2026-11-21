"""Environment-driven settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_KEYS: dict[str, str] = {
    "workflow-broker": "demo-key-workflow-broker",
    "prometheus": "demo-key-prometheus",
    "pricing-logs": "demo-key-pricing-logs",
    "trade-gateway": "demo-key-trade-gateway",
    "pr-review-agent": "demo-key-pr-review-agent",
    "custody": "demo-key-custody",
}


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    target_api_url: str = "http://localhost:8080"
    port: int = 8095
    base_rate: float = 2.0
    customer_amqp_url: str | None = None
    trade_queue: str = "vistula.trade-gateway"
    keys: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYS))
    prometheus_format: str = "alertmanager"
    auto_incidents: bool = True
    incidents_per_hour: float = 2.0
    timezone: str = "Europe/Warsaw"
    scenario_speed: float = 1.0
    baseline_enabled: bool = True
    max_retries: int = 5
    request_timeout: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        keys = dict(DEFAULT_KEYS)
        for name in keys:
            override = e.get("SOURCE_KEY_" + name.upper().replace("-", "_"))
            if override:
                keys[name] = override
        fmt = e.get("PROMETHEUS_FORMAT", "alertmanager").strip().lower()
        return cls(
            target_api_url=e.get("TARGET_API_URL", cls.target_api_url).rstrip("/"),
            port=int(e.get("PORT", cls.port)),
            base_rate=float(e.get("BASE_RATE", cls.base_rate)),
            customer_amqp_url=e.get("CUSTOMER_AMQP_URL") or None,
            trade_queue=e.get("TRADE_QUEUE", cls.trade_queue),
            keys=keys,
            prometheus_format=fmt if fmt in {"alertmanager", "single"} else "alertmanager",
            auto_incidents=_env_bool(e.get("AUTO_INCIDENTS"), cls.auto_incidents),
            incidents_per_hour=float(e.get("INCIDENTS_PER_HOUR", cls.incidents_per_hour)),
            timezone=e.get("SIM_TIMEZONE", cls.timezone),
            scenario_speed=max(float(e.get("SCENARIO_SPEED", cls.scenario_speed)), 0.001),
            baseline_enabled=_env_bool(e.get("BASELINE_ENABLED"), cls.baseline_enabled),
            max_retries=int(e.get("MAX_RETRIES", cls.max_retries)),
            request_timeout=float(e.get("REQUEST_TIMEOUT", cls.request_timeout)),
        )
