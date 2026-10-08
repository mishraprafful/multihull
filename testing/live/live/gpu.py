from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from live.capture import modal_dashboard_url, modal_state
from live.harness import LiveDeployment
from live.summary import GpuCost, ModalInfo, RunSummary
from live.sweep import list_apps, stale_apps

GPU = "L4"
L4_HOURLY_USD = 0.80
PRICE_DATE = "2026-10-08"
PRICE_SOURCE = "https://modal.com/pricing"
BUDGET_USD = 5.0
DEFAULT_MAX_MINUTES = 20
MAX_MINUTES_ENV = "LIVE_MAX_MINUTES"
API_KEYS_ENV = "LIVE_API_KEYS"
DEPLOY_READY_SECONDS = 15 * 60
EJECTION_SECONDS = 180
ROUTER_TUNING: dict[str, dict[str, bool | int | float | str]] = {"timeouts": {"first_byte": 30}}
READY_HEALTH = "ready"


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, max_minutes: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_minutes = max_minutes
        self.clock = clock
        self.started = clock()

    @property
    def max_seconds(self) -> float:
        return self.max_minutes * 60.0

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    @property
    def exceeded(self) -> bool:
        return self.elapsed() >= self.max_seconds

    def bounded(self, seconds: float) -> float:
        return max(0.0, min(seconds, self.remaining()))

    def check(self) -> None:
        if self.exceeded:
            raise BudgetExceeded(
                f"max_minutes {self.max_minutes} exceeded after {self.elapsed() / 60:.1f} minutes;"
                " aborting so the deployment is destroyed"
            )


def max_minutes_from_env(env: dict[str, str] | os._Environ[str] = os.environ) -> int:
    value = env.get(MAX_MINUTES_ENV, "").strip()
    minutes = int(value) if value else DEFAULT_MAX_MINUTES
    if minutes < 1:
        raise ValueError(f"{MAX_MINUTES_ENV} must be at least 1, got {minutes}")
    return minutes


def new_api_key() -> str:
    return f"hull_live{secrets.token_hex(4)}_{secrets.token_urlsafe(24)}"


def served_model(document: dict[str, Any]) -> str:
    command = [str(part) for part in document["container"]["command"]]
    for flag in ("--served-model-name", "--model"):
        if flag in command:
            return command[command.index(flag) + 1]
    raise ValueError("container.command names no model")


def new_cost(targets: int, max_minutes: int) -> GpuCost:
    return GpuCost(
        gpu=GPU,
        targets=targets,
        hourly_usd=L4_HOURLY_USD,
        price_date=PRICE_DATE,
        price_source=PRICE_SOURCE,
        budget_usd=BUDGET_USD,
        max_minutes=max_minutes,
    )


def worst_case_usd(targets: int, job_minutes: int) -> float:
    return targets * job_minutes / 60 * L4_HOURLY_USD


def modal_info(deployment: LiveDeployment, provider: str) -> ModalInfo:
    ref = deployment.refs().get(provider)
    ids = ref.ids if ref is not None else {}
    app_name = ids.get("app", f"multihull-{deployment.service}-{provider}")
    environment = ids.get("environment", "main")
    return ModalInfo(
        app_name=app_name,
        environment=environment,
        provider=provider,
        web_url=ids.get("web_url"),
        dashboard_url=modal_dashboard_url(app_name, environment),
    )


def capture_modal_apps(
    summary: RunSummary,
    deployment: LiveDeployment,
    label: str,
    lister: Callable[[str], list[dict[str, Any]]] = list_apps,
) -> None:
    for provider in deployment.providers_of_type("modal"):
        info = summary.modal_app(provider)
        if info is None:
            info = modal_info(deployment, provider)
            summary.modal_apps.append(info)
        if any(state.label == label for state in info.states):
            continue
        state, app_id = modal_state(info.app_name, info.environment, label, lister)
        info.states.append(state)
        info.app_id = info.app_id or app_id


def running_apps(
    prefix: str, environment: str, lister: Callable[[str], list[dict[str, Any]]] = list_apps
) -> list[str]:
    now = datetime.now(UTC) + timedelta(seconds=1)
    return sorted(
        str(app["description"])
        for app in stale_apps(lister(environment), prefix, timedelta(0), now)
    )


def ejected(entry: dict[str, Any]) -> bool:
    probe = entry.get("probe") or {}
    return (
        entry.get("health") != READY_HEALTH
        or entry.get("circuit") == "open"
        or probe.get("state") == "down"
    )
