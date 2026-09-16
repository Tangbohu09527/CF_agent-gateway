"""Finite transport limits and a separate wall-clock request budget (seconds)."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

import httpx


@dataclass(frozen=True, slots=True)
class HermesTimeoutSettings:
    connect_seconds: float = 5.0
    read_seconds: float = 600.0
    write_seconds: float = 15.0
    pool_seconds: float = 5.0
    execution_seconds: float = 600.0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            maximum = 3600 if field.name in {"read_seconds", "execution_seconds"} else 120
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= maximum
                or not math.isfinite(value)
            ):
                raise ValueError(f"hermes.timeouts.{field.name} must be > 0 and <= {maximum}")
            object.__setattr__(self, field.name, float(value))

    def httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_seconds,
            read=self.read_seconds,
            write=self.write_seconds,
            pool=self.pool_seconds,
        )
