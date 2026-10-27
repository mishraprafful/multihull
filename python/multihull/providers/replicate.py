from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx

from multihull.build_context import ContextScan, scan_context
from multihull.credentials import CredentialError, CredentialSource, read_token, token_origin
from multihull.providers.base import (
    CredHealth,
    DestroyDeferred,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    ScaleRefused,
    Target,
    credential_health,
    redact_env,
    redact_token,
)
from multihull.spec import GPU, ReplicateBlock

API_HOST = "api.replicate.com"
API_URL = f"https://{API_HOST}/v1"
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
HTTP_TIMEOUT_SECONDS = 15.0
PUSH_TIMEOUT_SECONDS = 3600.0
LOGIN_TIMEOUT_SECONDS = 60.0
PERMANENT_SCALE_STATUSES = frozenset({400, 422})
DEFERRED_DELETE_STATUSES = frozenset({400, 409, 422})
CONFLICT_STATUS = 409
DEPLOYMENT_FIELDS = ("version", "hardware", "min_instances", "max_instances")
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


class DeletionDeferred(DestroyDeferred):
    pass


class ModelLocks:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}

    def of(self, model: str) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(model, threading.Lock())


MODEL_LOCKS = ModelLocks()


def replicate_block(desired: Target) -> ReplicateBlock:
    return desired.target.replicate or ReplicateBlock()


def model_name(desired: Target) -> str:
    return desired.resource_name


def deployment_name(desired: Target) -> str:
    return f"{desired.resource_name}-{desired.provider}"


def build_context(desired: Target) -> str:
    build = desired.service.container.build
    return build.context if build else "."


def context_path(desired: Target) -> Path:
    written = Path(build_context(desired))
    if written.is_absolute():
        return written
    return (desired.service.source_dir or Path.cwd()) / written


def credential_files(desired: Target) -> list[Path]:
    sources = (
        CredentialSource.parse(t.credentials) for t in desired.service.targets if t.credentials
    )
    return [source.path for source in sources if source.kind == "file"]


def context_scan(desired: Target) -> ContextScan | None:
    context = context_path(desired)
    if not context.is_dir():
        return None
    return scan_context(context, credential_files(desired))


def context_notes(desired: Target, scan: ContextScan | None) -> list[str]:
    context = context_path(desired)
    if scan is None:
        return [f"build.context {context} is not a directory; apply will fail"]
    return [
        f"build.context {context} {problem}; exclude it in .dockerignore"
        for problem in scan.problems
    ]


def checked_scan(context: Path, protected: list[Path]) -> ContextScan:
    if not context.is_dir():
        raise ValueError(f"build.context {context} is not a directory")
    scan = scan_context(context, protected)
    if scan.problems:
        raise ValueError(
            f"build.context {context} " + "; ".join(scan.problems) + "; exclude them in "
            ".dockerignore or move the build context"
        )
    return scan


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


def plan_notes(desired: Target, scan: ContextScan | None) -> list[str]:
    notes = [*refusals(desired), *context_notes(desired, scan)]
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


def render_payload(desired: Target, scan: ContextScan | None) -> dict[str, Any]:
    owner = replicate_block(desired).owner or OWNER_PLACEHOLDER
    hardware = next(iter(hardware_choices(desired)), "")
    return {
        "model": render_model(owner, model_name(desired), hardware),
        "push": {
            "image": image_name(owner, model_name(desired)),
            "context": build_context(desired),
            "digest": scan.digest if scan else None,
        },
        "deployment": render_deployment(desired, owner, VERSION_PLACEHOLDER, hardware),
    }


def release_of(deployment: Mapping[str, Any]) -> dict[str, Any]:
    return dict(deployment.get("current_release") or {})


def live_settings(deployment: Mapping[str, Any]) -> dict[str, Any]:
    release = release_of(deployment)
    return {"version": release.get("version"), **(release.get("configuration") or {})}


def deployment_drift(deployment: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    live = live_settings(deployment)
    return {field: body[field] for field in DEPLOYMENT_FIELDS if live.get(field) != body[field]}


def redact(text: str, credentials: str | None = None) -> str:
    return redact_token(redact_env(text, [API_TOKEN_ENV, CLI_AUTH_TOKEN_ENV]), credentials)


def api_token(credentials: str | None = None) -> str:
    try:
        return read_token(credentials, API_TOKEN_ENV)
    except CredentialError as exc:
        raise CredentialError(f"{exc}; Replicate credentials missing") from None


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def traffic_headers(credentials: str | None = None) -> dict[str, str]:
    try:
        return bearer(api_token(credentials))
    except CredentialError:
        return {}


def traffic_note(credentials: str | None) -> str:
    return "" if credentials is None else ", which also signs router traffic"


def api_url(path: str) -> str:
    if not path.startswith(("http://", "https://")):
        return f"{API_URL}{path}"
    url = httpx.URL(path)
    if url.host != API_HOST:
        raise RuntimeError(f"refusing to follow a Replicate link to {url.host or path}")
    return str(url.copy_with(scheme="https", port=None))


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
    def __init__(self, http: httpx.Client, credentials: str | None = None) -> None:
        self.http = http
        self.credentials = credentials

    def call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        response = self.http.request(
            method, api_url(path), json=body, headers=bearer(api_token(self.credentials))
        )
        if missing_ok and response.status_code == 404:
            return None
        if response.is_error:
            raise ReplicateApiError(
                response.status_code, redact(problem_detail(response), self.credentials)
            )
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
        credentials: str | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.provider_name = provider_name
        self._http = http_client
        self.cog = cog or CogCli()
        self.credentials = credentials

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    @property
    def api(self) -> ReplicateApi:
        return ReplicateApi(self.http, self.credentials)

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        scan = context_scan(desired)
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_payload(desired, scan),
            notes=plan_notes(desired, scan),
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        refused = refusals(desired)
        if refused:
            raise ValueError("; ".join(refused))
        if self.dry_run:
            owner = replicate_block(desired).owner or OWNER_PLACEHOLDER
            return self.ref(desired, owner, {})
        context = context_path(desired)
        scan = checked_scan(context, credential_files(desired))
        owner = self.owner(desired)
        hardware = self.pick_hardware(desired)
        name = model_name(desired)
        with MODEL_LOCKS.of(f"{owner}/{name}"):
            self.ensure_model(owner, name, hardware)
            version = self.version(observed, owner, name, context, scan.digest)
        self.ensure_deployment(render_deployment(desired, owner, version, hardware), owner)
        return self.ref(desired, owner, {VERSION_KEY: version, CONTEXT_KEY: scan.digest})

    def ensure_model(self, owner: str, name: str, hardware: str) -> None:
        if self.api.model(owner, name) is not None:
            return
        try:
            self.api.call("POST", "/models", render_model(owner, name, hardware))
        except ReplicateApiError as exc:
            if exc.status != CONFLICT_STATUS:
                raise

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
                f"replicate.owner is {wanted} but "
                f"{token_origin(self.credentials, API_TOKEN_ENV)} belongs to {account}; "
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
            if exc.status not in DEFERRED_DELETE_STATUSES:
                raise
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
            return Observed(phase="Unknown", message=redact(str(exc), self.credentials))
        if current is None:
            return Observed(
                phase="Failed", message=f"Replicate deployment {owner}/{name} not found"
            )
        release = release_of(current)
        settings = live_settings(current)
        warm = int(settings.get("min_instances") or 0)
        if int(settings.get("max_instances") or 0) == 0:
            return Observed(
                "Failed",
                0,
                0,
                "max_instances is 0; out of rotation until Replicate allows deletion",
            )
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
        return Endpoint(
            url=deployment_url(owner, name), inject_headers=traffic_headers(self.credentials)
        )

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in REPLICATE_HARDWARE]

    def credentials_health(self) -> CredHealth:
        present = credential_health(self.credentials, API_TOKEN_ENV, self.type)
        if not present.ok:
            return present
        origin = token_origin(self.credentials, API_TOKEN_ENV)
        try:
            account = self.api.account()
        except ReplicateApiError as exc:
            if exc.status == 401:
                return CredHealth(ok=False, message=f"Replicate rejected {origin}: {exc}")
            return CredHealth(ok=False, message=f"could not verify {origin}: {exc}")
        except httpx.HTTPError as exc:
            reason = f"could not reach Replicate: {type(exc).__name__}: {exc}"
            return CredHealth(ok=False, message=redact(reason, self.credentials))
        username = str(account.get("username") or "")
        if not self.cog.available():
            return CredHealth(
                ok=False,
                message=f"Replicate accepted {origin}, but cog is not on PATH; "
                "install Cog to push the model",
                identity=username,
            )
        return CredHealth(
            ok=True,
            message=f"Replicate accepted {origin}{traffic_note(self.credentials)}",
            identity=username,
        )

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
