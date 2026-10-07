from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from e2e.client import Outcome
from e2e.harness import Router
from live.harness import PRIMARY, Kind, LiveDeployment
from live.summary import (
    ImageBuild,
    KubeSnapshot,
    ModalInfo,
    ModalState,
    Pod,
    RouterState,
    RunSummary,
    Scenario,
)
from live.sweep import list_apps
from multihull.providers.base import SERVICE_LABEL

WIDE_KINDS = "deployment,replicaset,pod,service,endpointslice"
EVENT_COUNT = 15
AFTER_DEPLOY = "after deploy"
AFTER_FAILOVER = "after failover"
BEFORE_DESTROY = "before destroy"
AFTER_DESTROY = "after destroy"
IMAGE_LOG_TAIL_LINES = 60


def kubectl_json(kind: Kind, *args: str) -> dict[str, Any]:
    result = kind.kubectl(*args, "-o", "json")
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return dict(json.loads(result.stdout))


def pod_ready(item: dict[str, Any]) -> tuple[int, int]:
    statuses = (item.get("status") or {}).get("containerStatuses") or []
    total = len((item.get("spec") or {}).get("containers") or [])
    return sum(1 for s in statuses if s.get("ready")), total


def pods_from(document: dict[str, Any]) -> list[Pod]:
    pods: list[Pod] = []
    for item in document.get("items") or []:
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        statuses = status.get("containerStatuses") or []
        containers = (item.get("spec") or {}).get("containers") or [{}]
        ready, total = pod_ready(item)
        phase = "Terminating" if metadata.get("deletionTimestamp") else status.get("phase", "")
        pods.append(
            Pod(
                name=metadata.get("name", ""),
                phase=phase,
                ready=f"{ready}/{total}",
                restarts=sum(int(s.get("restartCount") or 0) for s in statuses),
                image=statuses[0].get("image", "") if statuses else containers[0].get("image", ""),
                image_id=statuses[0].get("imageID", "") if statuses else "",
                node=(item.get("spec") or {}).get("nodeName", ""),
            )
        )
    return pods


def event_time(event: dict[str, Any]) -> str:
    stamp = (
        event.get("lastTimestamp")
        or event.get("eventTime")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )
    return str(stamp)


def events_from(document: dict[str, Any], count: int = EVENT_COUNT) -> list[dict[str, str]]:
    items = sorted(document.get("items") or [], key=event_time)[-count:]
    return [
        {
            "time": event_time(item).replace("T", " ").removesuffix("Z")[-8:],
            "type": str(item.get("type", "")),
            "reason": str(item.get("reason", "")),
            "object": "/".join(
                str((item.get("involvedObject") or {}).get(key, "")) for key in ("kind", "name")
            ),
            "message": str(item.get("message", "")),
        }
        for item in items
    ]


def node_port(service: dict[str, Any]) -> int | None:
    ports = (service.get("spec") or {}).get("ports") or []
    return int(ports[0]["nodePort"]) if ports and ports[0].get("nodePort") else None


def kube_snapshot(kind: Kind, service: str, label: str, router_url: str | None) -> KubeSnapshot:
    try:
        wide = kind.kubectl("get", WIDE_KINDS, "-o", "wide")
        rollout = kind.kubectl("rollout", "status", f"deployment/{service}", "--watch=false")
        pods = kubectl_json(kind, "get", "pods", "-l", f"{SERVICE_LABEL}={service}")
        services = kubectl_json(kind, "get", "services", "-l", f"{SERVICE_LABEL}={service}")
        events = kubectl_json(kind, "get", "events")
        not_ready = [
            item["metadata"]["name"]
            for item in pods.get("items") or []
            if pod_ready(item)[0] < pod_ready(item)[1]
        ]
        describe = kind.kubectl("describe", "pods", *not_ready).stdout if not_ready else ""
        first_service = (services.get("items") or [{}])[0]
        return KubeSnapshot(
            label=label,
            namespace=kind.namespace,
            wide=(wide.stdout + wide.stderr).strip(),
            rollout=(rollout.stdout + rollout.stderr).strip(),
            pods=pods_from(pods),
            node_port=node_port(first_service),
            router_url=router_url,
            events=events_from(events),
            describe=describe,
        )
    except Exception as exc:
        return KubeSnapshot(label=label, namespace=kind.namespace, error=str(exc))


def newest_app(apps: Sequence[dict[str, Any]], app_name: str) -> dict[str, Any] | None:
    matching = [app for app in apps if app.get("description") == app_name]
    return max(matching, key=lambda app: str(app.get("created_at") or ""), default=None)


def modal_state(
    app_name: str, environment: str, label: str, lister: Callable[[str], list[dict[str, Any]]]
) -> tuple[ModalState, str | None]:
    now = datetime.now(UTC).strftime("%H:%M:%S UTC")
    try:
        app = newest_app(lister(environment), app_name)
    except Exception as exc:
        return ModalState(label=label, state="unknown", error=str(exc)), None
    if app is None:
        return ModalState(label=label, state="not listed", at=now), None
    tasks = app.get("tasks")
    return (
        ModalState(
            label=label,
            state=str(app.get("state", "")),
            tasks=int(tasks) if str(tasks or "").isdigit() else None,
            at=now,
        ),
        str(app.get("app_id") or "") or None,
    )


def modal_dashboard_url(app_name: str, environment: str) -> str | None:
    try:
        import modal

        return str(modal.App.lookup(app_name, environment_name=environment).get_dashboard_url())
    except Exception:
        return None


def totals_by(metrics: Any, name: str, label: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for sample in metrics.series(name):
        key = sample.labels.get(label, "")
        totals[key] = totals.get(key, 0.0) + sample.value
    return totals


def router_state(router: Router, label: str) -> RouterState:
    try:
        response = router.http.get(f"{router.admin_url}/debug/endpoints")
        response.raise_for_status()
        document = response.json()
        metrics = router.metrics()
    except Exception as exc:
        return RouterState(label=label, error=str(exc))
    endpoints = [
        {
            "id": e.get("id"),
            "provider": e.get("provider"),
            "health": e.get("health"),
            "circuit": e.get("circuit"),
            "probe": {
                "state": (e.get("probe") or {}).get("state"),
                "last_status": (e.get("probe") or {}).get("last_status"),
            },
        }
        for e in document.get("endpoints") or []
    ]
    return RouterState(
        label=label,
        snapshot_version=document.get("snapshot_version"),
        endpoints=endpoints,
        requests_by_outcome=totals_by(metrics, "router_requests_total", "outcome"),
        failovers_by_reason=totals_by(metrics, "router_failovers_total", "reason"),
    )


class Recorder:
    def __init__(self, summary: RunSummary, workdir: Path) -> None:
        self.summary = summary
        self.workdir = workdir

    def save(self) -> None:
        self.summary.save(self.workdir)

    def capture(
        self,
        label: str,
        kind: Kind,
        deployment: LiveDeployment,
        router: Router | None = None,
        kube: bool = True,
    ) -> None:
        if kube and not self.summary.has_kube(label):
            self.summary.kube.append(
                kube_snapshot(kind, deployment.service, label, primary_url(deployment))
            )
        if deployment.secondary_type == "modal" and not self.summary.has_modal_state(label):
            self.capture_modal(label, deployment)
        if router is not None:
            self.summary.router = router_state(router, label)
        self.save()

    def capture_modal(self, label: str, deployment: LiveDeployment) -> None:
        info = self.summary.modal or self.modal_info(deployment)
        state, app_id = modal_state(info.app_name, info.environment, label, list_apps)
        info.states.append(state)
        info.app_id = info.app_id or app_id
        self.summary.modal = info

    def capture_image_builds(self, deployment: LiveDeployment) -> None:
        if not deployment.image_builds:
            return
        info = self.summary.modal or self.modal_info(deployment)
        known = {build.image_id for build in info.image_builds}
        for image_id, path in deployment.image_builds.items():
            if image_id in known:
                continue
            lines = path.read_text(errors="replace").splitlines()
            info.image_builds.append(
                ImageBuild(
                    image_id=image_id,
                    log=str(path.relative_to(deployment.workdir)),
                    tail="\n".join(lines[-IMAGE_LOG_TAIL_LINES:]),
                )
            )
        self.summary.modal = info
        self.save()

    def modal_info(self, deployment: LiveDeployment) -> ModalInfo:
        ref = deployment.refs().get(deployment.secondary)
        ids = ref.ids if ref is not None else {}
        app_name = ids.get("app", f"multihull-{deployment.service}")
        environment = ids.get("environment", "main")
        return ModalInfo(
            app_name=app_name,
            environment=environment,
            web_url=ids.get("web_url"),
            dashboard_url=modal_dashboard_url(app_name, environment),
        )


def primary_url(deployment: LiveDeployment) -> str | None:
    try:
        return str(deployment.snapshot_endpoints()[PRIMARY]["url"])
    except Exception:
        return None


class ScenarioProbe:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.router: Router | None = None
        self.failovers_at_start = 0.0

    def watch(self, router: Router) -> None:
        self.router = router
        self.failovers_at_start = router.metrics().failovers()

    def add(self, outcomes: Sequence[Outcome]) -> None:
        self.scenario.requests += len(outcomes)
        self.scenario.client_errors += sum(1 for outcome in outcomes if not outcome.ok)
        for outcome in outcomes:
            provider = outcome.provider or "none"
            self.scenario.providers[provider] = self.scenario.providers.get(provider, 0) + 1

    def note(self, text: str) -> None:
        self.scenario.note = text

    def finish(self) -> None:
        if self.router is None:
            return
        try:
            failovers = self.router.metrics().failovers() - self.failovers_at_start
            self.scenario.failovers = int(round(failovers))
        except Exception:
            self.scenario.failovers = None
