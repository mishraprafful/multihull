from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_PREFIX = "MOCK_"

HealthStatus = Literal["200", "503", "hang"]


class Knobs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttft_ms: float = Field(default=50, ge=0)
    tokens_per_sec: float = Field(default=200, gt=0)
    n_tokens: int = Field(default=20, ge=0)
    response_text: str = "the quick brown fox jumps over the lazy dog"
    error_rate: float = Field(default=0.0, ge=0, le=1)
    capacity_429_rate: float = Field(default=0.0, ge=0, le=1)
    max_inflight: int = Field(default=0, ge=0)
    disconnect_after_chunks: int = Field(default=0, ge=0)
    health_status: HealthStatus = "200"
    hang_seconds: float = Field(default=120, ge=0)
    warming: bool = False
    connect_refuse: bool = False

    @field_validator("health_status", mode="before")
    @classmethod
    def coerce_health_status(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) else value

    def merged(self, updates: Mapping[str, Any]) -> Knobs:
        return Knobs.model_validate({**self.model_dump(), **updates})


def env_name(knob: str) -> str:
    return ENV_PREFIX + knob.upper()


def knobs_from_env(environ: Mapping[str, str] | None = None) -> Knobs:
    source = os.environ if environ is None else environ
    overrides = {
        knob: source[env_name(knob)] for knob in Knobs.model_fields if env_name(knob) in source
    }
    return Knobs.model_validate(overrides)
