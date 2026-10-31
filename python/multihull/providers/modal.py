from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import httpx

from multihull.credentials import CredentialError, CredentialSource
from multihull.providers.base import (
    SERVICE_LABEL,
    CredHealth,
    EdgeError,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    Target,
    redact_env,
    redact_values,
    resolve_secret_values,
    secret_env_name,
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
IMAGE_BUILDER_VERSION = "2025.06"
IMAGE_BUILDER_VERSION_ENV = "MODAL_IMAGE_BUILDER_VERSION"
RESET_ENTRYPOINT = "ENTRYPOINT []"
HEALTH_PATH_KEY = "health_path"
DEFAULT_HEALTH_PATH = "/health"
HEALTH_TIMEOUT_SECONDS = 5.0
EDGE_ERROR = EdgeError(statuses=(404,), body_prefix="modal-http:")
DEFAULT_PROFILE = "default"
PROFILE_ENV = "MODAL_PROFILE"
CONFIG_PATH_ENV = "MODAL_CONFIG_PATH"
NATIVE_CREDENTIAL_PREFIXES = ("MODAL_TOKEN_", "MODAL_OAUTH_")


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


def redact(text: str, env_names: Iterable[str] = (), tokens: Iterable[str] = ()) -> str:
    return redact_values(redact_env(text, [TOKEN_ID_ENV, TOKEN_SECRET_ENV, *env_names]), tokens)


def profile_name(document: dict[str, Any], source: CredentialSource) -> str:
    if not document or not all(isinstance(profile, dict) for profile in document.values()):
        raise CredentialError(f"credentials {source} must hold only .modal.toml profile tables")
    active = [name for name, profile in document.items() if profile.get("active") is True]
    if len(active) > 1:
        raise CredentialError(f"credentials {source} has more than one active profile")
    if active:
        return active[0]
    if list(document) == [DEFAULT_PROFILE]:
        return DEFAULT_PROFILE
    ignored = bool(os.environ.get(PROFILE_ENV))
    raise CredentialError(
        f"credentials {source} has no active profile; mark one with active = true "
        f"or keep a single profile named {DEFAULT_PROFILE}"
        + (f"; {PROFILE_ENV} does not pick an override's profile" if ignored else "")
    )


def profile_tokens(credentials: str) -> tuple[str, str, str]:
    source = CredentialSource.parse(credentials)
    try:
        document = tomllib.loads(source.read())
    except tomllib.TOMLDecodeError:
        raise CredentialError(f"credentials {source} is not .modal.toml TOML") from None
    name = profile_name(document, source)
    profile = document[name]
    if not (profile.get("token_id") and profile.get("token_secret")):
        raise CredentialError(
            f"profile {name} of credentials {source} has no token_id and token_secret; "
            "only token profiles are supported, not OAuth"
        )
    return name, str(profile["token_id"]), str(profile["token_secret"])


def modal_tokens(credentials: str) -> tuple[str, str]:
    _, token_id, token_secret = profile_tokens(credentials)
    return token_id, token_secret


def credential_tokens(credentials: str | None) -> list[str]:
    if credentials is None:
        return []
    try:
        return list(modal_tokens(credentials))
    except CredentialError:
        return []


def token_environment(credentials: str | None, config_dir: str) -> dict[str, str] | None:
    if credentials is None:
        return None
    token_id, token_secret = modal_tokens(credentials)
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name != PROFILE_ENV and not name.startswith(NATIVE_CREDENTIAL_PREFIXES)
    }
    return {
        **inherited,
        CONFIG_PATH_ENV: str(Path(config_dir) / ".modal.toml"),
        TOKEN_ID_ENV: token_id,
        TOKEN_SECRET_ENV: token_secret,
    }


def secret_env_names(spec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    if spec["image"]["secret"]:
        names.append(spec["image"]["secret"]["passwordEnv"])
    if spec["secret"]:
        names.extend(secret_env_name(key) for key in spec["secret"]["keys"])
    return names


async def hello_from_env(modal: Any) -> None:
    client = await modal.Client.from_env.aio()
    await client.hello.aio()


def hello_with_tokens(token_id: str, token_secret: str) -> Callable[[Any], Awaitable[None]]:
    async def hello(modal: Any) -> None:
        client = await modal.Client.from_credentials.aio(token_id, token_secret)
        await client.hello.aio()

    return hello


def check_credentials(
    timeout: float = CREDENTIALS_TIMEOUT_SECONDS, credentials: str | None = None
) -> CredentialCheck:
    if credentials is None:
        source = credentials_source()
        if source is None:
            return CredentialCheck(
                CredentialStatus.MISSING, f"no {ENV_CREDENTIALS} and no {CONFIG_CREDENTIALS}"
            )
        return verify_credentials(source, hello_from_env, [], timeout)
    try:
        name, token_id, token_secret = profile_tokens(credentials)
    except CredentialError as exc:
        return CredentialCheck(CredentialStatus.MISSING, str(exc))
    source = f"profile {name} of {CredentialSource.parse(credentials)}"
    tokens = [token_id, token_secret]
    return verify_credentials(source, hello_with_tokens(*tokens), tokens, timeout)


def verify_credentials(
    source: str,
    hello: Callable[[Any], Awaitable[None]],
    tokens: list[str],
    timeout: float,
) -> CredentialCheck:
    try:
        import modal
    except ImportError:
        return CredentialCheck(
            CredentialStatus.SDK_MISSING,
            f"{source} found but the modal SDK is not installed; "
            "install multihull[modal] to verify them",
        )
    try:
        asyncio.run(asyncio.wait_for(hello(modal), timeout))
    except TimeoutError:
        return CredentialCheck(
            CredentialStatus.UNREACHABLE, f"no answer from Modal within {timeout:g} s"
        )
    except modal.exception.AuthError as exc:
        return CredentialCheck(
            CredentialStatus.REJECTED, redact(f"Modal rejected {source}: {exc}", tokens=tokens)
        )
    except (ValueError, modal.exception.InvalidError) as exc:
        return CredentialCheck(
            CredentialStatus.INVALID,
            redact(f"the modal SDK refused {source} before sending: {exc}", tokens=tokens),
        )
    except Exception as exc:
        return CredentialCheck(
            CredentialStatus.UNREACHABLE,
            redact(f"could not verify {source}: {type(exc).__name__}: {exc}", tokens=tokens),
        )
    return CredentialCheck(CredentialStatus.ACCEPTED, f"Modal accepted {source}")


def image_builder_version() -> str:
    return os.environ.get(IMAGE_BUILDER_VERSION_ENV) or IMAGE_BUILDER_VERSION


@contextmanager
def env_override(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def modal_block(desired: Target) -> ModalBlock:
    return desired.target.modal or ModalBlock()


def app_name(desired: Target) -> str:
    return f"{desired.resource_name}-{desired.provider}"


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
    name = app_name(desired)
    spec: dict[str, Any] = {
        "app_name": name,
        "environment": block.environment,
        "region": block.region,
        "tags": {SERVICE_LABEL: desired.name},
        "image": {
            "ref": desired.image_ref,
            "secret": registry_secret_names(block.registrySecret),
            "builder_version": image_builder_version(),
            "setup_dockerfile_commands": [RESET_ENTRYPOINT, *block.setupDockerfileCommands],
        },
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
            "label": name,
        },
        "command": list(container.command or []),
        "env": dict(sorted(container.env.items())),
        "secret": {"name": name, "keys": list(container.secrets)} if container.secrets else None,
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
    ids = {
        "app": app_name(desired),
        "environment": block.environment,
        HEALTH_PATH_KEY: desired.service.container.health.path,
    }
    if block.region:
        ids["region"] = block.region
    if web_url:
        ids["web_url"] = web_url
    return Ref(provider=desired.provider, type="modal", service=desired.name, ids=ids)


def derive_web_url(app_name: str, workspace: str, environment: str) -> str:
    suffix = "" if environment == "main" else f"-{environment}"
    return f"https://{workspace}{suffix}--{app_name}.modal.run"


def default_http_client() -> httpx.Client:
    return httpx.Client(timeout=HEALTH_TIMEOUT_SECONDS)


def server_instance(app_name: str, environment: str | None, client: Any = None) -> Any:
    import modal

    return modal.Cls.from_name(
        app_name, SERVER_CLASS_NAME, environment_name=environment, client=client
    )()


def deployed_web_url(app_name: str, environment: str | None, client: Any = None) -> str | None:
    import modal

    try:
        url = server_instance(app_name, environment, client).serve.get_web_url()
    except modal.exception.NotFoundError:
        return None
    return str(url) if url else None


def stop_app(app_name: str, environment: str, credentials: str | None = None) -> None:
    with tempfile.TemporaryDirectory() as config_dir:
        result = subprocess.run(
            [sys.executable, "-m", "modal", "app", "stop", app_name, "--env", environment, "--yes"],
            capture_output=True,
            text=True,
            check=False,
            env=token_environment(credentials, config_dir),
        )
    if result.returncode == 0:
        return
    output = f"{result.stdout}\n{result.stderr}".strip()
    if any(marker in output.lower() for marker in APP_GONE_MARKERS):
        return
    detail = output.splitlines()[-1] if output else f"exit code {result.returncode}"
    detail = redact(detail, tokens=credential_tokens(credentials))
    raise RuntimeError(f"modal app stop {app_name} failed: {detail}")


class ModalProvider:
    type: ClassVar = "modal"
    native_credential_env: ClassVar[tuple[str, ...]] = (
        TOKEN_ID_ENV,
        TOKEN_SECRET_ENV,
        PROXY_TOKEN_ID_ENV,
        PROXY_TOKEN_SECRET_ENV,
    )

    @staticmethod
    def native_credential_files() -> list[Path]:
        configured = os.environ.get(CONFIG_PATH_ENV)
        return [*([Path(configured)] if configured else []), Path.home() / ".modal.toml"]

    def __init__(
        self,
        dry_run: bool = True,
        workspace: str | None = None,
        provider_name: str = "modal",
        http_client: httpx.Client | None = None,
        credentials: str | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.workspace = workspace or os.environ.get("MODAL_WORKSPACE", "workspace")
        self.provider_name = provider_name
        self._http = http_client
        self.credentials = credentials
        self._client: Any = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = default_http_client()
        return self._http

    @property
    def client(self) -> Any:
        if self.credentials is None:
            return None
        if self._client is None:
            import modal

            self._client = modal.Client.from_credentials(*modal_tokens(self.credentials))
        return self._client

    def redact(self, text: str, env_names: Iterable[str] = ()) -> str:
        return redact(text, env_names, credential_tokens(self.credentials))

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
        try:
            web_url = deploy_with_sdk(spec, self.client)
        except Exception as exc:
            raise RuntimeError(self.redact(str(exc), secret_env_names(spec))) from None
        return ref_for(desired, web_url)

    def destroy(self, ref: Ref) -> None:
        if self.dry_run:
            return
        stop_app(ref.ids["app"], ref.ids.get("environment", "main"), self.credentials)

    def status(self, ref: Ref) -> Observed:
        if self.dry_run:
            return Observed(phase="Unknown", message="dry run")
        try:
            server = server_instance(ref.ids["app"], ref.ids.get("environment"), self.client)
            stats = server.serve.get_current_stats()
        except Exception as exc:
            return Observed(phase="Unknown", message=self.redact(str(exc)))
        running = int(getattr(stats, "num_total_runners", 0))
        if not running:
            return Observed(phase="Pending", message="no running container")
        endpoint = self.endpoint(ref)
        url = f"{endpoint.url}{ref.ids.get(HEALTH_PATH_KEY, DEFAULT_HEALTH_PATH)}"
        try:
            response = self.http.get(url, headers=endpoint.inject_headers)
        except httpx.HTTPError as exc:
            return Observed(
                phase="Pending",
                desired_replicas=running,
                message=f"{running} container(s) running, health probe {exc.__class__.__name__}",
            )
        if response.is_success:
            return Observed(
                phase="Ready",
                ready_replicas=running,
                desired_replicas=running,
                message=f"health {response.status_code}",
            )
        return Observed(
            phase="Pending",
            desired_replicas=running,
            message=f"{running} container(s) running, health returned {response.status_code}",
        )

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.dry_run:
            return
        server = server_instance(ref.ids["app"], ref.ids.get("environment"), self.client)
        server.update_autoscaler(min_containers=min, max_containers=max)

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        if self.dry_run:
            return
        yield from fetch_logs_with_sdk(
            ref.ids["app"], ref.ids.get("environment"), since, self.client
        )

    def endpoint(self, ref: Ref) -> Endpoint:
        url = ref.ids.get("web_url") or derive_web_url(
            ref.ids["app"], self.workspace, ref.ids.get("environment", "main")
        )
        headers: dict[str, str] = {}
        token_id = os.environ.get(PROXY_TOKEN_ID_ENV)
        token_secret = os.environ.get(PROXY_TOKEN_SECRET_ENV)
        if token_id and token_secret:
            headers = {"Modal-Key": token_id, "Modal-Secret": token_secret}
        return Endpoint(
            url=url,
            inject_headers=headers,
            region=ref.ids.get("region"),
            edge_error=EDGE_ERROR,
        )

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, region=None, available=True) for gpu in MODAL_GPU]

    def credentials_health(self) -> CredHealth:
        check = check_credentials(credentials=self.credentials)
        return CredHealth(ok=check.ok, message=check.message)

    def rediscover(self, service: str) -> Ref | None:
        if self.dry_run:
            return None
        try:
            import modal
        except ImportError:
            return None
        name = f"multihull-{service}-{self.provider_name}"
        try:
            modal.App.lookup(name, client=self.client)
        except Exception:
            return None
        ids = {"app": name}
        web_url = deployed_web_url(name, None, self.client)
        if web_url:
            ids["web_url"] = web_url
        return Ref(provider=self.provider_name, type="modal", service=service, ids=ids)


def fetch_logs_with_sdk(
    app_name: str, environment: str | None, since: timedelta, client: Any = None
) -> Iterator[str]:
    try:
        import modal
    except ImportError as exc:
        raise RuntimeError("modal SDK not installed; install multihull[modal]") from exc
    app = modal.App.lookup(app_name, environment_name=environment, client=client)
    for entry in app.logs.fetch(since=datetime.now(UTC) - since):
        context = entry.context_ids[-1] if entry.context_ids else app_name
        for line in entry.message.splitlines():
            yield f"{context} {line}"


def deploy_with_sdk(spec: dict[str, Any], client: Any = None) -> str:
    import modal

    registry_secret = spec["image"]["secret"]
    image = modal.Image.from_registry(
        spec["image"]["ref"],
        secret=modal.Secret.from_dict(registry_credentials(registry_secret))
        if registry_secret
        else None,
        setup_dockerfile_commands=spec["image"]["setup_dockerfile_commands"],
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

    with env_override(IMAGE_BUILDER_VERSION_ENV, spec["image"]["builder_version"]):
        app.deploy(name=spec["app_name"], environment_name=spec["environment"], client=client)
    return str(Server().serve.get_web_url())
