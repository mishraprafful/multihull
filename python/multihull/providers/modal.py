from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
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
from multihull.spec import GPU, ModalBlock, RegistrySecret

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
MIN_STARTUP_TIMEOUT_SECONDS = 60
SERVER_CLASS_NAME = "Server"
PROXY_TOKEN_ID_ENV = "MODAL_PROXY_TOKEN_ID"
PROXY_TOKEN_SECRET_ENV = "MODAL_PROXY_TOKEN_SECRET"
REGISTRY_USERNAME_KEY = "REGISTRY_USERNAME"
REGISTRY_PASSWORD_KEY = "REGISTRY_PASSWORD"
APP_GONE_MARKERS = ("already stopped", "no app with name")
MISSING_COMMAND_NOTE = (
    "container.command is empty; Modal does not run the image CMD, so set the server command"
)
TOKEN_ID_ENV = "MODAL_TOKEN_ID"
TOKEN_SECRET_ENV = "MODAL_TOKEN_SECRET"
ENV_CREDENTIALS = f"{TOKEN_ID_ENV}/{TOKEN_SECRET_ENV}"
CONFIG_CREDENTIALS = "~/.modal.toml"
CREDENTIALS_TIMEOUT_SECONDS = 10.0
REDACTED = "<redacted>"


class CredentialStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INVALID = "invalid"
    UNREACHABLE = "unreachable"
    SDK_MISSING = "sdk-missing"
    MISSING = "missing"


@dataclass(frozen=True)
class CredentialCheck:
    status: CredentialStatus
    message: str

    @property
    def ok(self) -> bool:
        return self.status is CredentialStatus.ACCEPTED


def credentials_source() -> str | None:
    if os.environ.get(TOKEN_ID_ENV) and os.environ.get(TOKEN_SECRET_ENV):
        return ENV_CREDENTIALS
    if (Path.home() / ".modal.toml").exists():
        return CONFIG_CREDENTIALS
    return None


def secret_fragments() -> list[str]:
    fragments: set[str] = set()
    for name in (TOKEN_ID_ENV, TOKEN_SECRET_ENV):
        value = os.environ.get(name, "")
        fragments.update({value, value.strip(), repr(value)[1:-1], *value.split()})
    return sorted((f for f in fragments if f), key=len, reverse=True)


def redact(text: str) -> str:
    for fragment in secret_fragments():
        text = text.replace(fragment, REDACTED)
    return text


async def hello_from_env(modal: Any) -> None:
    client = await modal.Client.from_env.aio()
    await client.hello.aio()


def check_credentials(timeout: float = CREDENTIALS_TIMEOUT_SECONDS) -> CredentialCheck:
    source = credentials_source()
    if source is None:
        return CredentialCheck(
            CredentialStatus.MISSING, f"no {ENV_CREDENTIALS} and no {CONFIG_CREDENTIALS}"
        )
    try:
        import modal
    except ImportError:
        return CredentialCheck(
            CredentialStatus.SDK_MISSING,
            f"{source} found but the modal SDK is not installed; "
            "install multihull[modal] to verify them",
        )
    try:
        asyncio.run(asyncio.wait_for(hello_from_env(modal), timeout))
    except TimeoutError:
        return CredentialCheck(
            CredentialStatus.UNREACHABLE, f"no answer from Modal within {timeout:g} s"
        )
    except modal.exception.AuthError as exc:
        return CredentialCheck(CredentialStatus.REJECTED, redact(f"Modal rejected {source}: {exc}"))
    except (ValueError, modal.exception.InvalidError) as exc:
        return CredentialCheck(
            CredentialStatus.INVALID,
            redact(f"the modal SDK refused {source} before sending: {exc}"),
        )
    except Exception as exc:
        return CredentialCheck(
            CredentialStatus.UNREACHABLE,
            redact(f"could not verify {source}: {type(exc).__name__}: {exc}"),
        )
    return CredentialCheck(CredentialStatus.ACCEPTED, f"Modal accepted {source}")


def modal_block(desired: Target) -> ModalBlock:
    return desired.target.modal or ModalBlock()


def modal_gpu(desired: Target) -> str | None:
    if not desired.gpus:
        return None
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
        "image": {"ref": desired.image_ref, "secret": registry_secret_names(block.registrySecret)},
        "gpu": modal_gpu(desired),
        "memory_mib": memory_to_mib(desired.service.resources.memory),
        "min_containers": replicas.min,
        "max_containers": replicas.max,
        "buffer_containers": 1 if replicas.max > replicas.min else 0,
        "scaledown_window": SCALEDOWN_WINDOW_SECONDS,
        "max_inputs": desired.service.scaling.concurrency,
        "web_server": {
            "port": container.port,
            "startup_timeout": max(
                container.health.initialDelaySeconds, MIN_STARTUP_TIMEOUT_SECONDS
            ),
            "label": desired.resource_name,
        },
        "command": list(container.command or []),
        "env": dict(sorted(container.env.items())),
        "secret": {"name": desired.resource_name, "keys": list(container.secrets)}
        if container.secrets
        else None,
    }
    return spec


def registry_secret_names(secret: RegistrySecret | None) -> dict[str, str] | None:
    if secret is None:
        return None
    return {"usernameEnv": secret.usernameEnv, "passwordEnv": secret.passwordEnv}


def registry_credentials(names: dict[str, str]) -> dict[str, str]:
    missing = [env for env in names.values() if not os.environ.get(env)]
    if missing:
        raise RuntimeError(
            "missing registry credentials in environment: " + ", ".join(sorted(missing))
        )
    return {
        REGISTRY_USERNAME_KEY: os.environ[names["usernameEnv"]],
        REGISTRY_PASSWORD_KEY: os.environ[names["passwordEnv"]],
    }


def plan_notes(desired: Target) -> list[str]:
    return [] if desired.service.container.command else [MISSING_COMMAND_NOTE]


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
    return f"https://{workspace}{suffix}--{app_name}.modal.run"


def server_instance(app_name: str, environment: str | None) -> Any:
    import modal

    return modal.Cls.from_name(app_name, SERVER_CLASS_NAME, environment_name=environment)()


def stop_app(app_name: str, environment: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "modal", "app", "stop", app_name, "--env", environment, "--yes"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    output = f"{result.stdout}\n{result.stderr}".strip()
    if any(marker in output.lower() for marker in APP_GONE_MARKERS):
        return
    detail = output.splitlines()[-1] if output else f"exit code {result.returncode}"
    raise RuntimeError(f"modal app stop {app_name} failed: {detail}")


class ModalProvider:
    type: ClassVar = "modal"

    def __init__(self, dry_run: bool = True, workspace: str | None = None) -> None:
        self.dry_run = dry_run
        self.workspace = workspace or os.environ.get("MODAL_WORKSPACE", "workspace")

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_app_spec(desired),
            notes=plan_notes(desired),
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        spec = render_app_spec(desired)
        if not spec["command"]:
            raise ValueError(MISSING_COMMAND_NOTE)
        if self.dry_run:
            return ref_for(desired)
        web_url = deploy_with_sdk(spec)
        return ref_for(desired, web_url)

    def destroy(self, ref: Ref) -> None:
        if self.dry_run:
            return
        stop_app(ref.ids["app"], ref.ids.get("environment", "main"))

    def status(self, ref: Ref) -> Observed:
        if self.dry_run:
            return Observed(phase="Unknown", message="dry run")
        try:
            server = server_instance(ref.ids["app"], ref.ids.get("environment"))
            stats = server.serve.get_current_stats()
        except Exception as exc:
            return Observed(phase="Unknown", message=str(exc))
        ready = int(getattr(stats, "num_total_runners", 0))
        return Observed(phase="Ready" if ready else "Pending", ready_replicas=ready)

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.dry_run:
            return
        server = server_instance(ref.ids["app"], ref.ids.get("environment"))
        server.update_autoscaler(min_containers=min, max_containers=max)

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        if self.dry_run:
            return
        yield from fetch_logs_with_sdk(ref.ids["app"], ref.ids.get("environment"), since)

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
        check = check_credentials()
        return CredHealth(ok=check.ok, message=check.message)

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


def fetch_logs_with_sdk(app_name: str, environment: str | None, since: timedelta) -> Iterator[str]:
    try:
        import modal
    except ImportError as exc:
        raise RuntimeError("modal SDK not installed; install multihull[modal]") from exc
    app = modal.App.lookup(app_name, environment_name=environment)
    for entry in app.logs.fetch(since=datetime.now(UTC) - since):
        context = entry.context_ids[-1] if entry.context_ids else app_name
        for line in entry.message.splitlines():
            yield f"{context} {line}"


def deploy_with_sdk(spec: dict[str, Any]) -> str:
    import modal

    registry_secret = spec["image"]["secret"]
    image = modal.Image.from_registry(
        spec["image"]["ref"],
        secret=modal.Secret.from_dict(registry_credentials(registry_secret))
        if registry_secret
        else None,
    )
    if spec["env"]:
        image = image.env(spec["env"])
    app = modal.App(spec["app_name"], image=image)
    secrets = []
    if spec["secret"]:
        secrets.append(modal.Secret.from_dict(resolve_secret_values(spec["secret"]["keys"])))
    command = spec["command"]
    port = spec["web_server"]["port"]

    @app.cls(
        serialized=True,
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

        @modal.web_server(
            port=port,
            startup_timeout=spec["web_server"]["startup_timeout"],
            label=spec["web_server"]["label"],
        )
        def serve(self) -> None:
            return None

    app.deploy(name=spec["app_name"], environment_name=spec["environment"])
    return str(Server().serve.get_web_url())
