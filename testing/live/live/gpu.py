from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable, Sequence
from typing import Any

from e2e.client import Outcome
from live.capture import modal_dashboard_url, modal_state
from live.harness import Kind, LiveDeployment
from live.summary import GpuCost, ModalInfo, RunSummary
from live.sweep import list_apps

GPU = "L4"
MODAL_HOURLY_USD = {"L4": 0.80, "A10G": 1.10, "A100-40": 2.10, "A100-80": 2.50, "H100": 3.95}
PRICE_DATE = "2026-10-26"
PRICE_SOURCE = "https://modal.com/pricing"
BUDGET_USD = 5.0
DEFAULT_MAX_MINUTES = 20
MAX_MINUTES_ENV = "LIVE_MAX_MINUTES"
API_KEYS_ENV = "LIVE_API_KEYS"
DEPLOY_READY_SECONDS = 20 * 60
EJECTION_SECONDS = 180
ROLLOUT_SECONDS = 15 * 60
CIRCUIT_CLOSE_SECONDS = 180
MODAL_URL_SUFFIX = ".modal.run"
CLUSTER_DNS_SUFFIX = ".svc.cluster.local"
ROUTER_TUNING: dict[str, dict[str, bool | int | float | str]] = {"timeouts": {"first_byte": 30}}
READY_HEALTH = "ready"
ROUTABLE_HEALTH = frozenset({READY_HEALTH, "degraded", "draining", "unspecified"})


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


def new_cost(targets: int, max_minutes: int, gpu: str = GPU) -> GpuCost:
    return GpuCost(
        gpu=gpu,
        targets=targets,
        hourly_usd=MODAL_HOURLY_USD[gpu],
        price_date=PRICE_DATE,
        price_source=PRICE_SOURCE,
        budget_usd=BUDGET_USD,
        max_minutes=max_minutes,
    )


def worst_case_usd(targets: int, job_minutes: int, gpu: str = GPU) -> float:
    return targets * job_minutes / 60 * MODAL_HOURLY_USD[gpu]


def gpu_class(document: dict[str, Any]) -> str:
    classes = document["resources"].get("gpu") or [GPU]
    return str(classes[0])


def primary_type(document: dict[str, Any]) -> str:
    primary = min(document["targets"], key=lambda t: int(t.get("priority", 0)))
    return str(primary["type"])


def restores_primary(document: dict[str, Any]) -> bool:
    return primary_type(document) == "kubernetes"


def stop_primary(deployment: LiveDeployment, cluster: Kind | None) -> str:
    if deployment.primary_type == "modal":
        return f"modal app stop {deployment.stop_modal_app(deployment.primary)}"
    if deployment.primary_type == "kubernetes" and cluster is not None:
        cluster.scale(deployment.service, 0)
        return f"kubectl scale deployment/{deployment.service} --replicas=0"
    raise ValueError(f"cannot stop a {deployment.primary_type} primary")


def reachable_from_runner(provider_type: str, url: str) -> bool:
    if provider_type == "modal":
        return url.endswith(MODAL_URL_SUFFIX)
    return url.startswith(("http://", "https://")) and CLUSTER_DNS_SUFFIX not in url


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


def edge_not_found(outcomes: Sequence[Outcome]) -> list[str]:
    return [outcome.describe() for outcome in outcomes if outcome.status == 404]


def taking_traffic(entry: dict[str, Any]) -> bool:
    probe = entry.get("probe") or {}
    return (
        entry.get("health") == READY_HEALTH
        and entry.get("circuit") in (None, "closed")
        and probe.get("state") == "up"
    )


def ejected(entry: dict[str, Any]) -> bool:
    probe = entry.get("probe") or {}
    return (
        entry.get("health") not in ROUTABLE_HEALTH
        or entry.get("circuit") == "open"
        or probe.get("state") == "down"
    )
