from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from docker.errors import NotFound

from multihull.providers.base import (
    PROVIDER_LABEL,
    SERVICE_LABEL,
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    Target,
    resolve_secret_values,
    secret_env_name,
)
from multihull.spec import DockerBlock

OWNER_LABEL = "multihull.dev/owner"
CONFIG_HASH_LABEL = "multihull.dev/config-hash"
HEALTH_PATH_LABEL = "multihull.dev/health-path"
OWNER = "multihull"
DEFAULT_HOST = "127.0.0.1"
HEALTH_TIMEOUT_SECONDS = 2.0
HEALTH_INTERVAL_SECONDS = 10
HEALTH_RETRIES = 3
STOP_TIMEOUT_SECONDS = 10
NANOSECONDS = 1_000_000_000
RUNNING_STATES = frozenset({"running"})
STARTING_STATES = frozenset({"created", "restarting", "paused"})
DOCKER_HEALTH_STARTING = "starting"


def docker_block(desired: Target) -> DockerBlock:
    return desired.target.docker or DockerBlock()


def container_name(desired: Target) -> str:
    return f"{desired.resource_name}-{desired.provider}"


def healthcheck(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    url = f"http://localhost:{container.port}{container.health.path}"
    return {
        "test": ["CMD-SHELL", f"curl -fsS {url} || wget -qO- {url} || exit 1"],
        "interval": HEALTH_INTERVAL_SECONDS * NANOSECONDS,
        "timeout": int(HEALTH_TIMEOUT_SECONDS) * NANOSECONDS,
        "retries": HEALTH_RETRIES,
        "start_period": container.health.initialDelaySeconds * NANOSECONDS,
    }


def render_run_kwargs(desired: Target) -> dict[str, Any]:
    block = docker_block(desired)
    container = desired.service.container
    kwargs: dict[str, Any] = {
        "image": block.image or desired.image_ref,
        "command": list(container.command) if container.command else None,
        "name": container_name(desired),
        "detach": True,
        "ports": {f"{container.port}/tcp": (block.host, block.hostPort or None)},
        "environment": {**dict(sorted(container.env.items())), **dict(sorted(block.env.items()))},
        "labels": {
            SERVICE_LABEL: desired.name,
            PROVIDER_LABEL: desired.provider,
            OWNER_LABEL: OWNER,
            HEALTH_PATH_LABEL: container.health.path,
        },
        "restart_policy": {"Name": "unless-stopped"},
        "healthcheck": healthcheck(desired),
    }
    if block.network:
        kwargs["network"] = block.network
    return kwargs


def plan_notes(desired: Target) -> list[str]:
    if not desired.gpus:
        return []
    classes = ", ".join(g.value for g in desired.gpus)
    return [f"resources.gpu [{classes}] ignored: docker targets run the container without GPUs"]


def config_hash(kwargs: dict[str, Any], secret_names: list[str]) -> str:
    canonical = json.dumps(
        {"run": kwargs, "secrets": sorted(secret_names)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def secret_environment(secret_names: list[str]) -> dict[str, str]:
    values = resolve_secret_values(secret_names)
    return {secret_env_name(name): value for name, value in values.items()}


def first_host_binding(bindings: list[dict[str, Any]]) -> tuple[str, int] | None:
    for binding in bindings:
        port = binding.get("HostPort")
        if port:
            return binding.get("HostIp") or DEFAULT_HOST, int(port)
    return None


def bound_host_port(attrs: dict[str, Any], container_port: int) -> tuple[str, int] | None:
    key = f"{container_port}/tcp"
    live = ((attrs.get("NetworkSettings") or {}).get("Ports") or {}).get(key) or []
    requested = ((attrs.get("HostConfig") or {}).get("PortBindings") or {}).get(key) or []
    return first_host_binding(live) or first_host_binding(requested)


def exposed_container_port(attrs: dict[str, Any]) -> int | None:
    bindings = (attrs.get("HostConfig") or {}).get("PortBindings") or {}
    exposed = (attrs.get("Config") or {}).get("ExposedPorts") or {}
    for key in list(bindings) + list(exposed):
        return int(key.split("/")[0])
    return None


def probe_failure_phase(state: dict[str, Any]) -> str:
    docker_health = (state.get("Health") or {}).get("Status") or DOCKER_HEALTH_STARTING
    return "Pending" if docker_health == DOCKER_HEALTH_STARTING else "Failed"


def ref_from_container(
    container: Any, service: str, provider: str, container_port: int | None, host: str
) -> Ref:
    attrs = container.attrs or {}
    port = container_port or exposed_container_port(attrs)
    bound = bound_host_port(attrs, port) if port is not None else None
    bound_host, bound_port = bound if bound else (host, 0)
    labels = container.labels or {}
    return Ref(
        provider=provider,
        type="docker",
        service=service,
        ids={
            "container": container.id,
            "name": container.name,
            "host": bound_host,
            "host_port": str(bound_port),
            "health_path": labels.get(HEALTH_PATH_LABEL, "/health"),
        },
    )


def default_client() -> Any:
    import docker

    return docker.from_env()


def default_http_client() -> httpx.Client:
    return httpx.Client(timeout=HEALTH_TIMEOUT_SECONDS)


class DockerProvider:
    type: ClassVar = "docker"

    def __init__(
        self,
        client: Any | None = None,
        http_client: httpx.Client | None = None,
        client_factory: Callable[[], Any] | None = None,
        provider_name: str | None = None,
    ) -> None:
        self._client = client
        self._http = http_client
        self._client_factory = client_factory
        self.provider_name = provider_name

    @property
    def client(self) -> Any:
        if self._client is None:
            factory = self._client_factory or default_client
            self._client = factory()
        return self._client

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = default_http_client()
        return self._http

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_run_kwargs(desired),
            notes=plan_notes(desired),
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        block = docker_block(desired)
        container_port = desired.service.container.port
        secrets = desired.service.container.secrets
        kwargs = render_run_kwargs(desired)
        digest = config_hash(kwargs, secrets)
        existing = self._find_by_name(kwargs["name"])
        if existing is not None:
            if (existing.labels or {}).get(CONFIG_HASH_LABEL) == digest:
                if existing.status not in RUNNING_STATES:
                    existing.start()
                    existing.reload()
                return ref_from_container(
                    existing, desired.name, desired.provider, container_port, block.host
                )
            existing.remove(force=True)
        if block.pull:
            self.client.images.pull(kwargs["image"])
        run_kwargs = dict(kwargs)
        run_kwargs["labels"] = {**kwargs["labels"], CONFIG_HASH_LABEL: digest}
        if secrets:
            run_kwargs["environment"] = {**kwargs["environment"], **secret_environment(secrets)}
        container = self.client.containers.run(**run_kwargs)
        container.reload()
        return ref_from_container(
            container, desired.name, desired.provider, container_port, block.host
        )

    def destroy(self, ref: Ref) -> None:
        container = self._get(ref)
        if container is None:
            return
        try:
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
            container.remove(force=True)
        except NotFound:
            return

    def status(self, ref: Ref) -> Observed:
        container = self._get(ref)
        if container is None:
            return Observed(phase="Unknown", desired_replicas=1, message="container not found")
        container.reload()
        state = (container.attrs or {}).get("State") or {}
        docker_status = state.get("Status") or container.status or "unknown"
        if docker_status in STARTING_STATES:
            return Observed("Pending", 0, 1, f"container {docker_status}")
        if docker_status not in RUNNING_STATES:
            exit_code = state.get("ExitCode")
            return Observed("Failed", 0, 1, f"container {docker_status} (exit code {exit_code})")
        url = f"{self.endpoint(ref).url}{ref.ids.get('health_path', '/health')}"
        try:
            response = self.http.get(url)
        except httpx.HTTPError as exc:
            phase = probe_failure_phase(state)
            return Observed(phase, 0, 1, f"health probe failed: {exc.__class__.__name__}")
        if response.is_success:
            return Observed("Ready", 1, 1, f"health {response.status_code}")
        return Observed(probe_failure_phase(state), 0, 1, f"health returned {response.status_code}")

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if min != 1 or max != 1:
            raise ValueError(
                f"docker target {ref.provider} runs exactly one container; "
                f"cannot scale to min={min} max={max}"
            )

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        container = self._get(ref)
        if container is None:
            return
        start = datetime.now(UTC) - since
        stream = container.logs(since=start, stream=True, follow=False, stdout=True, stderr=True)
        for chunk in stream:
            yield from chunk.decode(errors="replace").splitlines()

    def endpoint(self, ref: Ref) -> Endpoint:
        return Endpoint(url=f"http://{ref.ids['host']}:{ref.ids['host_port']}")

    def gpu_inventory(self) -> list[GPUOffer]:
        return []

    def credentials_health(self) -> CredHealth:
        try:
            self.client.ping()
            version = str(self.client.version().get("Version", "unknown"))
        except Exception as exc:
            return CredHealth(ok=False, message=f"docker daemon unreachable: {exc}")
        return CredHealth(ok=True, message=f"docker daemon {version} reachable", identity=version)

    def rediscover(self, service: str) -> Ref | None:
        selectors = [f"{SERVICE_LABEL}={service}", f"{OWNER_LABEL}={OWNER}"]
        if self.provider_name:
            selectors.append(f"{PROVIDER_LABEL}={self.provider_name}")
        containers = self.client.containers.list(all=True, filters={"label": selectors})
        if not containers:
            return None
        container = containers[0]
        provider = (container.labels or {}).get(PROVIDER_LABEL, "docker")
        return ref_from_container(container, service, provider, None, DEFAULT_HOST)

    def _find_by_name(self, name: str) -> Any | None:
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _get(self, ref: Ref) -> Any | None:
        for key in ("container", "name"):
            identifier = ref.ids.get(key)
            if not identifier:
                continue
            try:
                return self.client.containers.get(identifier)
            except NotFound:
                continue
        return None
