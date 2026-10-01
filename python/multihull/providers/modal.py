from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

from multihull.providers.base import (
    SERVICE_LABEL,
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    Target,
    resolve_secret_values,
)
from multihull.spec import GPU, ModalBlock

MODAL_GPU: dict[GPU, str] = {
    GPU.L4: "L4",
    GPU.A10G: "A10G",
    GPU.A100_40: "A100-40GB",
    GPU.A100_80: "A100-80GB",
    GPU.H100: "H100",
    GPU.H200: "H200",
    GPU.B200: "B200",
}
SCALEDOWN_WINDOW_SECONDS = 300
WEB_FUNCTION_NAME = "serve"
PROXY_TOKEN_ID_ENV = "MODAL_PROXY_TOKEN_ID"
PROXY_TOKEN_SECRET_ENV = "MODAL_PROXY_TOKEN_SECRET"


def modal_block(desired: Target) -> ModalBlock:
    return desired.target.modal or ModalBlock()


def modal_gpu(desired: Target) -> str:
    name = MODAL_GPU[desired.gpus[0]]
    count = desired.service.resources.gpuCount
    return name if count == 1 else f"{name}:{count}"


def render_app_spec(desired: Target) -> dict[str, Any]:
    block = modal_block(desired)
    container = desired.service.container
    replicas = desired.replicas
    spec: dict[str, Any] = {
        "app_name": desired.resource_name,
        "environment": block.environment,
        "region": block.region,
        "tags": {SERVICE_LABEL: desired.name},
        "image": {"ref": desired.image_ref, "secret": None},
        "gpu": modal_gpu(desired),
        "memory_mib": memory_to_mib(desired.service.resources.memory),
        "min_containers": replicas.min,
        "max_containers": replicas.max,
        "buffer_containers": 1 if replicas.max > replicas.min else 0,
        "scaledown_window": SCALEDOWN_WINDOW_SECONDS,
        "max_inputs": desired.service.scaling.concurrency,
        "web_server": {
            "port": container.port,
            "startup_timeout": container.health.initialDelaySeconds,
        },
        "command": list(container.command or []),
        "env": dict(sorted(container.env.items())),
        "secret": {"name": desired.resource_name, "keys": list(container.secrets)}
        if container.secrets
        else None,
    }
    return spec


def memory_to_mib(memory: str | None) -> int | None:
    if memory is None:
        return None
    units = {"Gi": 1024, "G": 1000, "Mi": 1, "M": 1}
    for suffix, factor in units.items():
        if memory.endswith(suffix):
            return int(float(memory[: -len(suffix)]) * factor)
    return int(memory) // (1024 * 1024)


def ref_for(desired: Target, web_url: str | None = None) -> Ref:
    block = modal_block(desired)
    ids = {"app": desired.resource_name, "environment": block.environment}
    if block.region:
        ids["region"] = block.region
    if web_url:
        ids["web_url"] = web_url
    return Ref(provider=desired.provider, type="modal", service=desired.name, ids=ids)


def derive_web_url(app_name: str, workspace: str, environment: str) -> str:
    suffix = "" if environment == "main" else f"-{environment}"
    return f"https://{workspace}{suffix}--{app_name}-{WEB_FUNCTION_NAME}.modal.run"


class ModalProvider:
    type: ClassVar = "modal"

    def __init__(self, dry_run: bool = True, workspace: str | None = None) -> None:
        self.dry_run = dry_run
        self.workspace = workspace or os.environ.get("MODAL_WORKSPACE", "workspace")

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(provider=desired.provider, type=self.type, payload=render_app_spec(desired))

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        spec = render_app_spec(desired)
        if self.dry_run:
            return ref_for(desired)
        web_url = deploy_with_sdk(spec)
        return ref_for(desired, web_url)

    def destroy(self, ref: Ref) -> None:
        if self.dry_run:
            return
        import modal

        try:
            app = modal.App.lookup(ref.ids["app"], environment_name=ref.ids.get("environment"))
            app.stop()
        except Exception:
            return

    def status(self, ref: Ref) -> Observed:
        if self.dry_run:
            return Observed(phase="Unknown", message="dry run")
        try:
            import modal
        except ImportError:
            return Observed(phase="Unknown", message="modal SDK not installed")
        try:
            fn = modal.Function.from_name(
                ref.ids["app"], WEB_FUNCTION_NAME, environment_name=ref.ids.get("environment")
            )
            stats = fn.get_current_stats()
        except Exception as exc:
            return Observed(phase="Unknown", message=str(exc))
        ready = int(getattr(stats, "num_total_runners", 0))
        return Observed(phase="Ready" if ready else "Pending", ready_replicas=ready)

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.dry_run:
            return
        import modal

        fn = modal.Function.from_name(
            ref.ids["app"], WEB_FUNCTION_NAME, environment_name=ref.ids.get("environment")
        )
        fn.update_autoscaler(min_containers=min, max_containers=max)

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        yield from ()

    def endpoint(self, ref: Ref) -> Endpoint:
        url = ref.ids.get("web_url") or derive_web_url(
            ref.ids["app"], self.workspace, ref.ids.get("environment", "main")
        )
        headers: dict[str, str] = {}
        token_id = os.environ.get(PROXY_TOKEN_ID_ENV)
        token_secret = os.environ.get(PROXY_TOKEN_SECRET_ENV)
        if token_id and token_secret:
            headers = {"Modal-Key": token_id, "Modal-Secret": token_secret}
        return Endpoint(url=url, inject_headers=headers, region=ref.ids.get("region"))

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, region=None, available=True) for gpu in MODAL_GPU]

    def credentials_health(self) -> CredHealth:
        if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
            return CredHealth(ok=True, message="MODAL_TOKEN_ID and MODAL_TOKEN_SECRET are set")
        if (Path.home() / ".modal.toml").exists():
            return CredHealth(ok=True, message="~/.modal.toml present")
        return CredHealth(
            ok=False, message="no MODAL_TOKEN_ID/MODAL_TOKEN_SECRET and no ~/.modal.toml"
        )

    def rediscover(self, service: str) -> Ref | None:
        if self.dry_run:
            return None
        try:
            import modal
        except ImportError:
            return None
        app_name = f"multihull-{service}"
        try:
            modal.App.lookup(app_name)
        except Exception:
            return None
        return Ref(provider="modal", type="modal", service=service, ids={"app": app_name})


def deploy_with_sdk(spec: dict[str, Any]) -> str:
    import subprocess

    import modal

    image = modal.Image.from_registry(spec["image"]["ref"])
    if spec["env"]:
        image = image.env(spec["env"])
    app = modal.App(spec["app_name"], image=image)
    secrets = []
    if spec["secret"]:
        secrets.append(modal.Secret.from_dict(resolve_secret_values(spec["secret"]["keys"])))
    command = spec["command"]
    port = spec["web_server"]["port"]

    @app.cls(
        gpu=spec["gpu"],
        memory=spec["memory_mib"],
        min_containers=spec["min_containers"],
        max_containers=spec["max_containers"],
        buffer_containers=spec["buffer_containers"],
        scaledown_window=spec["scaledown_window"],
        secrets=secrets,
        region=spec["region"],
    )
    @modal.concurrent(max_inputs=spec["max_inputs"] or 1)
    class Server:
        @modal.enter()
        def start(self) -> None:
            self.process = subprocess.Popen(command)

        @modal.web_server(port=port, startup_timeout=spec["web_server"]["startup_timeout"])
        def serve(self) -> None:
            return None

    app.deploy(name=spec["app_name"], environment_name=spec["environment"])
    return str(Server().serve.get_web_url())
