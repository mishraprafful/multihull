from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx

from multihull.providers.base import (
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    ScaleRefused,
    Target,
    env_credential_health,
    redact_env,
)
from multihull.spec import GPU, ReplicateBlock

API_URL = "https://api.replicate.com/v1"
REGISTRY = "r8.im"
API_TOKEN_ENV = "REPLICATE_API_TOKEN"
CLI_AUTH_TOKEN_ENV = "REPLICATE_CLI_AUTH_TOKEN"
REPLICATE_HARDWARE: dict[GPU, str] = {
    GPU.L4: "gpu-l40s",
    GPU.A10G: "gpu-a40-small",
    GPU.A100_40: "gpu-a100-large",
    GPU.A100_80: "gpu-a100-large",
    GPU.H100: "gpu-h100",
}
CPU_HARDWARE = "cpu"
MODEL_VISIBILITY = "private"
MAX_MIN_INSTANCES = 5
MAX_MAX_INSTANCES = 20
SYNC_PREFERENCE = "wait=60"
HTTP_TIMEOUT_SECONDS = 15.0
PUSH_TIMEOUT_SECONDS = 3600.0
LOGIN_TIMEOUT_SECONDS = 60.0
PERMANENT_SCALE_STATUSES = frozenset({400, 422})
DEPLOYMENT_FIELDS = ("version", "hardware", "min_instances", "max_instances")
IGNORED_CONTEXT_DIRS = frozenset({".git", ".cog", "__pycache__"})
READ_CHUNK = 1 << 20
OWNER_PLACEHOLDER = "{owner}"
VERSION_PLACEHOLDER = "{version}"
OWNER_KEY = "owner"
MODEL_KEY = "model"
DEPLOYMENT_KEY = "deployment"
VERSION_KEY = "version"
CONTEXT_KEY = "context"


class ReplicateApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Replicate API {status}: {detail}")
        self.status = status


class DeletionDeferred(RuntimeError):
    pass


def replicate_block(desired: Target) -> ReplicateBlock:
    return desired.target.replicate or ReplicateBlock()


def model_name(desired: Target) -> str:
    return desired.resource_name


def deployment_name(desired: Target) -> str:
    return f"{desired.resource_name}-{desired.provider}"


def build_context(desired: Target) -> str:
    build = desired.service.container.build
    return build.context if build else "."


def image_name(owner: str, model: str) -> str:
    return f"{REGISTRY}/{owner}/{model}"


def deployment_url(owner: str, deployment: str) -> str:
    return f"{API_URL}/deployments/{owner}/{deployment}"


def hardware_choices(desired: Target) -> list[str]:
    if not desired.gpus:
        return [CPU_HARDWARE]
    return list(
        dict.fromkeys(REPLICATE_HARDWARE[gpu] for gpu in desired.gpus if gpu in REPLICATE_HARDWARE)
    )


def unsupported_gpus(desired: Target) -> list[GPU]:
    return [gpu for gpu in desired.gpus if gpu not in REPLICATE_HARDWARE]


def no_hardware_message(gpus: Sequence[GPU]) -> str:
    names = ", ".join(gpu.value for gpu in gpus)
    supported = ", ".join(gpu.value for gpu in REPLICATE_HARDWARE)
    return f"Replicate has no hardware for {names}; resources.gpu needs one of {supported}"


def refusals(desired: Target) -> list[str]:
    container = desired.service.container
    replicas = desired.replicas
    found: list[str] = []
    if not hardware_choices(desired):
        found.append(no_hardware_message(unsupported_gpus(desired)))
    if container.env or container.secrets:
        names = ", ".join([*sorted(container.env), *container.secrets])
        found.append(
            f"Replicate deployments take no environment, so container env and secrets ({names}) "
            "cannot reach the model; build them into the Cog image or drop the Replicate target"
        )
    if replicas.min > MAX_MIN_INSTANCES:
        found.append(
            f"Replicate allows at most {MAX_MIN_INSTANCES} min_instances, not {replicas.min}"
        )
    if replicas.max > MAX_MAX_INSTANCES:
        found.append(
            f"Replicate allows at most {MAX_MAX_INSTANCES} max_instances, not {replicas.max}"
        )
    return found


def plan_notes(desired: Target) -> list[str]:
    notes = refusals(desired)
    missing = unsupported_gpus(desired)
    if missing and hardware_choices(desired):
        names = ", ".join(gpu.value for gpu in missing)
        notes.append(f"Replicate has no hardware for {names}; those classes are skipped")
    if desired.service.container.command:
        notes.append("container.command is ignored; the Cog image runs its predictor")
    return notes


def render_deployment(desired: Target, owner: str, version: str, hardware: str) -> dict[str, Any]:
    replicas = desired.replicas
    return {
        "name": deployment_name(desired),
        "model": f"{owner}/{model_name(desired)}",
        "version": version,
        "hardware": hardware,
        "min_instances": replicas.min,
        "max_instances": replicas.max,
    }


def render_model(owner: str, name: str, hardware: str) -> dict[str, Any]:
    return {"owner": owner, "name": name, "visibility": MODEL_VISIBILITY, "hardware": hardware}


def render_payload(desired: Target) -> dict[str, Any]:
    owner = replicate_block(desired).owner or OWNER_PLACEHOLDER
    hardware = next(iter(hardware_choices(desired)), "")
    return {
        "model": render_model(owner, model_name(desired), hardware),
        "push": {
            "image": image_name(owner, model_name(desired)),
            "context": build_context(desired),
        },
        "deployment": render_deployment(desired, owner, VERSION_PLACEHOLDER, hardware),
    }


def context_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d not in IGNORED_CONTEXT_DIRS)
        linked = [d for d in subdirs if (Path(directory) / d).is_symlink()]
        for name in sorted([*files, *linked]):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(f"L {relative} {os.readlink(path)}\n".encode())
                continue
            digest.update(f"F {relative}\n".encode())
            with path.open("rb") as handle:
                while chunk := handle.read(READ_CHUNK):
                    digest.update(chunk)
    return digest.hexdigest()[:16]


def release_of(deployment: Mapping[str, Any]) -> dict[str, Any]:
    return dict(deployment.get("current_release") or {})


def live_settings(deployment: Mapping[str, Any]) -> dict[str, Any]:
    release = release_of(deployment)
    return {"version": release.get("version"), **(release.get("configuration") or {})}


def deployment_drift(deployment: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    live = live_settings(deployment)
    return {field: body[field] for field in DEPLOYMENT_FIELDS if live.get(field) != body[field]}


def redact(text: str) -> str:
    return redact_env(text, [API_TOKEN_ENV, CLI_AUTH_TOKEN_ENV])


def api_token() -> str:
    value = os.environ.get(API_TOKEN_ENV, "").strip()
    if not value:
        raise RuntimeError(f"{API_TOKEN_ENV} is not set; Replicate credentials missing")
    return value


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def traffic_headers() -> dict[str, str]:
    value = os.environ.get(API_TOKEN_ENV, "").strip()
    return {**(bearer(value) if value else {}), "Prefer": SYNC_PREFERENCE}


def problem_detail(response: httpx.Response) -> str:
    try:
        problem = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    if not isinstance(problem, dict):
        return str(problem)[:200]
    title = str(problem.get("title") or response.reason_phrase)
    detail = str(problem.get("detail") or "")
    return f"{title}: {detail}" if detail else title


def last_lines(text: str, count: int = 5) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-count:])


Runner = Callable[..., subprocess.CompletedProcess[str]]


class CogCli:
    def __init__(self, binary: str = "cog", run: Runner = subprocess.run) -> None:
        self.binary = binary
        self.run = run

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def invoke(
        self, args: list[str], timeout: float, cwd: Path | None = None, stdin: str | None = None
    ) -> None:
        result = self.run(
            [self.binary, *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            output = last_lines(f"{result.stdout}\n{result.stderr}") or "no output"
            raise RuntimeError(redact(f"cog {args[0]} exited {result.returncode}: {output}"))

    def login(self, credential: str) -> None:
        self.invoke(["login", "--token-stdin"], LOGIN_TIMEOUT_SECONDS, stdin=credential)

    def push(self, image: str, context: Path) -> None:
        self.invoke(["push", image], PUSH_TIMEOUT_SECONDS, cwd=context)


class ReplicateApi:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        url = path if path.startswith("https://") else f"{API_URL}{path}"
        response = self.http.request(method, url, json=body, headers=bearer(api_token()))
        if missing_ok and response.status_code == 404:
            return None
        if response.is_error:
            raise ReplicateApiError(response.status_code, redact(problem_detail(response)))
        return response.json() if response.content else None

    def account(self) -> dict[str, Any]:
        return dict(self.call("GET", "/account"))

    def hardware(self) -> list[str]:
        return [str(item["sku"]) for item in self.call("GET", "/hardware") or []]

    def model(self, owner: str, name: str) -> dict[str, Any] | None:
        return self.call("GET", f"/models/{owner}/{name}", missing_ok=True)

    def deployment(self, owner: str, name: str) -> dict[str, Any] | None:
        return self.call("GET", f"/deployments/{owner}/{name}", missing_ok=True)

    def deployments(self) -> Iterator[dict[str, Any]]:
        page: str | None = "/deployments"
        while page:
            listing = self.call("GET", page) or {}
            yield from listing.get("results") or []
            page = listing.get("next")

    def deployment_named(self, name: str) -> dict[str, Any] | None:
        return next((d for d in self.deployments() if d.get("name") == name), None)


class ReplicateProvider:
    type: ClassVar = "replicate"

    def __init__(
        self,
        dry_run: bool = True,
        provider_name: str = "replicate",
        http_client: httpx.Client | None = None,
        cog: CogCli | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.provider_name = provider_name
        self._http = http_client
        self.cog = cog or CogCli()

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    @property
    def api(self) -> ReplicateApi:
        return ReplicateApi(self.http)

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_payload(desired),
            notes=plan_notes(desired),
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        refused = refusals(desired)
        if refused:
            raise ValueError("; ".join(refused))
        if self.dry_run:
            owner = replicate_block(desired).owner or OWNER_PLACEHOLDER
            return self.ref(desired, owner, {})
        owner = self.owner(desired)
        hardware = self.pick_hardware(desired)
        name = model_name(desired)
        if self.api.model(owner, name) is None:
            self.api.call("POST", "/models", render_model(owner, name, hardware))
        context = Path(build_context(desired))
        digest = context_digest(context)
        version = self.version(observed, owner, name, context, digest)
        self.ensure_deployment(render_deployment(desired, owner, version, hardware), owner)
        return self.ref(desired, owner, {VERSION_KEY: version, CONTEXT_KEY: digest})

    def ref(self, desired: Target, owner: str, extra: Mapping[str, str]) -> Ref:
        ids = {
            OWNER_KEY: owner,
            MODEL_KEY: f"{owner}/{model_name(desired)}",
            DEPLOYMENT_KEY: deployment_name(desired),
            **extra,
        }
        return Ref(provider=desired.provider, type=self.type, service=desired.name, ids=ids)

    def owner(self, desired: Target) -> str:
        account = str(self.api.account()["username"])
        wanted = replicate_block(desired).owner
        if wanted and wanted != account:
            raise ValueError(
                f"replicate.owner is {wanted} but {API_TOKEN_ENV} belongs to {account}; "
                "Replicate creates models and deployments only for the token's account"
            )
        return account

    def pick_hardware(self, desired: Target) -> str:
        offered = self.api.hardware()
        for sku in hardware_choices(desired):
            if sku in offered:
                return sku
        wanted = ", ".join(hardware_choices(desired))
        raise ValueError(f"Replicate offers none of {wanted}; available: {', '.join(offered)}")

    def version(
        self, observed: Ref | None, owner: str, name: str, context: Path, digest: str
    ) -> str:
        if (
            observed is not None
            and observed.ids.get(MODEL_KEY) == f"{owner}/{name}"
            and observed.ids.get(CONTEXT_KEY) == digest
            and observed.ids.get(VERSION_KEY)
        ):
            return observed.ids[VERSION_KEY]
        credential = os.environ.get(CLI_AUTH_TOKEN_ENV, "").strip()
        if credential:
            self.cog.login(credential)
        self.cog.push(image_name(owner, name), context)
        model = self.api.model(owner, name) or {}
        latest = model.get("latest_version") or {}
        if not latest.get("id"):
            raise RuntimeError(
                f"cog push finished but Replicate lists no version of {owner}/{name}"
            )
        return str(latest["id"])

    def ensure_deployment(self, body: Mapping[str, Any], owner: str) -> None:
        current = self.api.deployment(owner, body["name"])
        if current is None:
            self.api.call("POST", "/deployments", body)
            return
        serving = release_of(current).get("model")
        if serving and serving != body["model"]:
            raise RuntimeError(
                f"Replicate deployment {owner}/{body['name']} serves {serving}, not "
                f"{body['model']}; delete it or rename the target"
            )
        drift = deployment_drift(current, body)
        if drift:
            self.api.call("PATCH", f"/deployments/{owner}/{body['name']}", drift)

    def located(self, ref: Ref) -> tuple[str, str] | None:
        owner = ref.ids.get(OWNER_KEY)
        name = ref.ids.get(DEPLOYMENT_KEY)
        if not name or not owner or owner == OWNER_PLACEHOLDER:
            return None
        return owner, name

    def destroy(self, ref: Ref) -> None:
        if self.dry_run:
            return
        located = self.located(ref)
        if located is None:
            return
        owner, name = located
        current = self.api.deployment(owner, name)
        if current is None:
            return
        offline = {"min_instances": 0, "max_instances": 0}
        settings = live_settings(current)
        if any(settings.get(field) != value for field, value in offline.items()):
            self.api.call("PATCH", f"/deployments/{owner}/{name}", offline)
        try:
            self.api.call("DELETE", f"/deployments/{owner}/{name}", missing_ok=True)
        except ReplicateApiError as exc:
            raise DeletionDeferred(
                f"Replicate deployment {owner}/{name} is scaled to zero; Replicate deletes a "
                "deployment only after it has been offline and unused for 15 minutes, so run "
                f"destroy again after that ({exc})"
            ) from None

    def status(self, ref: Ref) -> Observed:
        if self.dry_run:
            return Observed(phase="Unknown", message="dry run")
        located = self.located(ref)
        if located is None:
            return Observed(phase="Unknown", message="no Replicate deployment in state")
        owner, name = located
        try:
            current = self.api.deployment(owner, name)
        except (httpx.HTTPError, RuntimeError) as exc:
            return Observed(phase="Unknown", message=redact(str(exc)))
        if current is None:
            return Observed(
                phase="Failed", message=f"Replicate deployment {owner}/{name} not found"
            )
        release = release_of(current)
        settings = live_settings(current)
        warm = int(settings.get("min_instances") or 0)
        running = str(settings.get("version") or "")
        applied = ref.ids.get(VERSION_KEY)
        if applied and running != applied:
            return Observed(
                "Pending", 0, warm, f"release runs version {running[:12]}, applied {applied[:12]}"
            )
        if warm < 1:
            return Observed("Pending", 0, 0, "min_instances is 0; no instance is kept warm")
        return Observed(
            "Ready",
            warm,
            warm,
            f"release {release.get('number')} keeps {warm} instance(s) warm on "
            f"{settings.get('hardware')}; Replicate reports no per-instance health",
        )

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.dry_run:
            return
        located = self.located(ref)
        if located is None:
            raise ScaleRefused("no Replicate deployment in state")
        if min > MAX_MIN_INSTANCES or max > MAX_MAX_INSTANCES:
            raise ScaleRefused(
                f"Replicate allows at most {MAX_MIN_INSTANCES} min_instances and "
                f"{MAX_MAX_INSTANCES} max_instances, asked for {min} and {max}"
            )
        owner, name = located
        try:
            self.api.call(
                "PATCH",
                f"/deployments/{owner}/{name}",
                {"min_instances": min, "max_instances": max},
            )
        except ReplicateApiError as exc:
            if exc.status in PERMANENT_SCALE_STATUSES:
                raise ScaleRefused(str(exc)) from None
            raise

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        yield from ()

    def endpoint(self, ref: Ref) -> Endpoint:
        owner = ref.ids.get(OWNER_KEY, OWNER_PLACEHOLDER)
        name = ref.ids.get(DEPLOYMENT_KEY, f"multihull-{ref.service}-{ref.provider}")
        return Endpoint(url=deployment_url(owner, name), inject_headers=traffic_headers())

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in REPLICATE_HARDWARE]

    def credentials_health(self) -> CredHealth:
        if not os.environ.get(API_TOKEN_ENV, "").strip():
            return env_credential_health(API_TOKEN_ENV, self.type)
        try:
            account = self.api.account()
        except ReplicateApiError as exc:
            if exc.status == 401:
                return CredHealth(ok=False, message=f"Replicate rejected {API_TOKEN_ENV}: {exc}")
            return CredHealth(ok=False, message=f"could not verify {API_TOKEN_ENV}: {exc}")
        except httpx.HTTPError as exc:
            return CredHealth(
                ok=False, message=redact(f"could not reach Replicate: {type(exc).__name__}: {exc}")
            )
        username = str(account.get("username") or "")
        if not self.cog.available():
            return CredHealth(
                ok=False,
                message=f"Replicate accepted {API_TOKEN_ENV}, but cog is not on PATH; "
                "install Cog to push the model",
                identity=username,
            )
        return CredHealth(ok=True, message=f"Replicate accepted {API_TOKEN_ENV}", identity=username)

    def rediscover(self, service: str) -> Ref | None:
        if self.dry_run:
            return None
        name = f"multihull-{service}-{self.provider_name}"
        found = self.api.deployment_named(name)
        if found is None:
            return None
        release = release_of(found)
        ids = {
            OWNER_KEY: str(found["owner"]),
            MODEL_KEY: str(release.get("model") or f"{found['owner']}/multihull-{service}"),
            DEPLOYMENT_KEY: name,
        }
        if release.get("version"):
            ids[VERSION_KEY] = str(release["version"])
        return Ref(provider=self.provider_name, type=self.type, service=service, ids=ids)
