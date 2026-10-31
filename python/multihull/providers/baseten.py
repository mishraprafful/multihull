from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import shlex
import tarfile
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

import httpx
import yaml

from multihull.credentials import CredentialError, read_token, token_origin
from multihull.providers.base import (
    PROVIDER_LABEL,
    SERVICE_LABEL,
    CredHealth,
    Endpoint,
    GPUOffer,
    Observed,
    Plan,
    Ref,
    ScaleRefused,
    Target,
    api_path,
    credential_health,
    mirrored_names,
    redact_env,
    redact_token,
    resolve_secret_values,
    store_name,
)
from multihull.spec import GPU

BASETEN_ACCELERATOR: dict[GPU, str] = {
    GPU.L4: "L4",
    GPU.A10G: "A10G",
    GPU.A100_40: "A100_40GB",
    GPU.A100_80: "A100",
    GPU.H100: "H100",
    GPU.H200: "H200",
    GPU.B200: "B200",
}
API_URL = "https://api.baseten.co/v1"
API_KEY_ENV = "BASETEN_API_KEY"
INFERENCE_API_KEY_ENV = "BASETEN_INFERENCE_API_KEY"
AUTH_SCHEME = "Api-Key"
ENVIRONMENT = "production"
SPEC_LABEL = "multihull.dev/spec"
HEALTH_PATH_LABEL = "multihull.dev/health-path"
SECRETS_LABEL = "multihull.dev/secrets"
LABELS_LIMIT_BYTES = 2048
SECRETS_DIR = "/secrets"
RESERVED_ENV = ("PORT", "HOST", "HOSTNAME", "PATH")
SCALE_DOWN_DELAY_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 30.0
UPLOAD_TIMEOUT_SECONDS = 120.0
HEALTH_TIMEOUT_SECONDS = 5.0
LOG_LIMIT = 1000
LOG_WINDOW = timedelta(days=7)
ACTIVE = "ACTIVE"
FAILED_STATUSES = frozenset({"BUILD_FAILED", "BUILD_STOPPED", "DEPLOY_FAILED", "FAILED"})
UNUSABLE_STATUSES = FAILED_STATUSES | {"DEACTIVATING", "INACTIVE"}
PERMANENT_SCALE_STATUSES = frozenset({400, 422})
DEFAULT_HEALTH_PATH = "/health"
ARCHIVE_CONFIG = "config.yaml"
S3_SERVICE = "s3"
SIGNING_ALGORITHM = "AWS4-HMAC-SHA256"
DNS_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
SHELL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
S3_ERROR_CODE = re.compile(r"<Code>([A-Za-z0-9.]{1,64})</Code>")
MODEL_KEY = "model"
DEPLOYMENT_KEY = "deployment"
NAME_KEY = "name"
URL_KEY = "url"
HEALTH_PATH_KEY = "health_path"
DIGEST_KEY = "spec"
SECRETS_KEY = "secrets"
DEPLOY_KEY_IN_SNAPSHOT = (
    f"{INFERENCE_API_KEY_ENV} is unset, so the router injects this workspace key: set an "
    "inference-only team key scoped to the production environment and this model"
)
NO_COMMAND = (
    "Baseten needs container.command: docker_server.start_command replaces the image "
    "entrypoint and is required for custom servers"
)
PERCENT_IN_COMMAND = (
    "Baseten writes docker_server.start_command into supervisord.conf, where % starts an "
    "interpolation and fails the build; remove % from container.command"
)
PERCENT_IN_SECRET_NAMES = (
    "Baseten passes secret names that are not shell identifiers to docker_server.start_command "
    "through exec env, where % fails the supervisord.conf build; remove % from container.secrets"
)


class BasetenApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Baseten API {status}: {detail}")
        self.status = status


def model_name(desired: Target) -> str:
    return f"{desired.resource_name}-{desired.provider}"


def endpoint_url(model_id: str) -> str:
    return f"https://model-{model_id}.api.baseten.co/environments/{ENVIRONMENT}/sync"


def secret_store_name(desired: Target, secret: str) -> str:
    return store_name(desired.name, desired.provider, secret)


def accelerator(desired: Target) -> str:
    name = BASETEN_ACCELERATOR[desired.gpus[0]]
    count = desired.service.resources.gpuCount
    return name if count == 1 else f"{name}:{count}"


def resources(desired: Target) -> dict[str, Any]:
    rendered: dict[str, Any] = (
        {"accelerator": accelerator(desired), "use_gpu": True}
        if desired.gpus
        else {"use_gpu": False}
    )
    if desired.service.resources.memory:
        rendered["memory"] = desired.service.resources.memory
    return rendered


def start_command(desired: Target) -> str | None:
    container = desired.service.container
    if not container.command:
        return None
    if not container.secrets:
        return shlex.join(container.command)
    exported = [name for name in container.secrets if SHELL_NAME.match(name)]
    passed = [name for name in container.secrets if not SHELL_NAME.match(name)]
    steps = [f"{name}={secret_read(desired, name)}; export {name}" for name in exported]
    assignments = "".join(
        f"{shlex.quote(name + '=')}{secret_read(desired, name)} " for name in passed
    )
    launch = f'exec env {assignments}"$@"' if passed else 'exec "$@"'
    return shlex.join(["sh", "-c", "; ".join([*steps, launch]), "sh", *container.command])


def secret_read(desired: Target, name: str) -> str:
    return f'"$(cat {SECRETS_DIR}/{secret_store_name(desired, name)})"'


def docker_server(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    server: dict[str, Any] = {}
    command = start_command(desired)
    if command is not None:
        server["start_command"] = command
    server.update(
        {
            "server_port": container.port,
            "predict_endpoint": "/v1/chat/completions"
            if desired.service.route.protocol == "openai"
            else "/",
            "readiness_endpoint": container.health.path,
            "liveness_endpoint": container.health.path,
        }
    )
    return server


def render_truss_config(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    return {
        "model_name": model_name(desired),
        "base_image": {"image": desired.image_ref},
        "docker_server": docker_server(desired),
        "resources": resources(desired),
        "runtime": {"predict_concurrency": desired.service.scaling.concurrency or 1},
        "environment_variables": dict(sorted(container.env.items())),
        "secrets": {secret_store_name(desired, name): None for name in container.secrets},
        "model_metadata": {SERVICE_LABEL: desired.name, PROVIDER_LABEL: desired.provider},
    }


def autoscaling_settings(desired: Target) -> dict[str, int]:
    replicas = desired.replicas
    return {
        "min_replica": replicas.min,
        "max_replica": replicas.max,
        "concurrency_target": desired.service.scaling.concurrency or 1,
        "scale_down_delay": SCALE_DOWN_DELAY_SECONDS,
    }


def config_digest(config: Mapping[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def deployment_labels(desired: Target, config: dict[str, Any]) -> dict[str, str]:
    labels = {
        SERVICE_LABEL: desired.name,
        PROVIDER_LABEL: desired.provider,
        SPEC_LABEL: config_digest(config),
        HEALTH_PATH_LABEL: desired.service.container.health.path,
    }
    if desired.service.container.secrets:
        labels[SECRETS_LABEL] = json.dumps(desired.service.container.secrets, separators=(",", ":"))
    return labels


def labelled_names(labels: Mapping[str, Any]) -> list[str]:
    try:
        names = json.loads(str(labels.get(SECRETS_LABEL) or "[]"))
    except ValueError:
        return []
    return [name for name in names if isinstance(name, str)] if isinstance(names, list) else []


def label_problems(desired: Target) -> list[str]:
    labels = deployment_labels(desired, render_truss_config(desired))
    size = len(json.dumps(labels, separators=(",", ":")).encode())
    if size <= LABELS_LIMIT_BYTES:
        return []
    return [
        f"deployment labels take {size} bytes of JSON, over Baseten's {LABELS_LIMIT_BYTES}-byte "
        "limit; shorten the service, provider or secret names, or declare fewer secrets"
    ]


def refusals(desired: Target) -> list[str]:
    return command_problems(desired) + label_problems(desired)


def render_deployment(desired: Target) -> dict[str, Any]:
    config = render_truss_config(desired)
    return {
        "config": config,
        "raw_config": yaml.safe_dump(config, sort_keys=False),
        "environment_name": ENVIRONMENT,
        "preserve_env_instance_type": False,
        "labels": deployment_labels(desired, config),
        "autoscaling_settings": autoscaling_settings(desired),
    }


def render_payload(desired: Target) -> dict[str, Any]:
    deployment = render_deployment(desired)
    return {
        "secrets": [
            {"name": secret_store_name(desired, name)} for name in desired.service.container.secrets
        ],
        "truss": deployment.pop("config"),
        "deployment": {key: value for key, value in deployment.items() if key != "raw_config"},
    }


def command_problems(desired: Target) -> list[str]:
    container = desired.service.container
    if not container.command:
        return [NO_COMMAND]
    problems = [PERCENT_IN_COMMAND] if any("%" in part for part in container.command) else []
    named = [name for name in container.secrets if "%" in name]
    if named:
        problems.append(f"{PERCENT_IN_SECRET_NAMES}: {', '.join(named)}")
    return problems


def plan_notes(desired: Target) -> list[str]:
    notes = refusals(desired)
    dropped = [name for name in RESERVED_ENV if name in desired.service.container.env]
    if dropped:
        notes.append(
            f"Baseten drops {', '.join(dropped)} from environment_variables and sets PORT=8080 "
            "for its proxy; the server must bind container.port without reading PORT"
        )
    return notes


def truss_archive(raw_config: str) -> bytes:
    data = raw_config.encode()
    info = tarfile.TarInfo(ARCHIVE_CONFIG)
    info.size = len(data)
    info.mode = 0o644
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def s3_location(bucket: str, key: str, region: str) -> tuple[str, str]:
    encoded = quote(key, safe="/~")
    if DNS_BUCKET.match(bucket):
        return f"{bucket}.s3.{region}.amazonaws.com", f"/{encoded}"
    return f"s3.{region}.amazonaws.com", f"/{quote(bucket, safe='')}/{encoded}"


def hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret_key: str, day: str, region: str) -> bytes:
    key = hmac_sha256(f"AWS4{secret_key}".encode(), day)
    for part in (region, S3_SERVICE, "aws4_request"):
        key = hmac_sha256(key, part)
    return key


def signed_s3_put(
    upload: Mapping[str, Any], body: bytes, now: datetime
) -> tuple[str, dict[str, str]]:
    creds = upload["creds"]
    region = str(upload["s3_region"])
    host, path = s3_location(str(upload["s3_bucket"]), str(upload["s3_key"]), region)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": stamp,
        "x-amz-security-token": str(creds["aws_session_token"]).strip(),
    }
    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        ["PUT", path, "", *(f"{name}:{headers[name]}" for name in names), "", signed_headers]
        + [payload_hash]
    )
    scope = f"{day}/{region}/{S3_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [SIGNING_ALGORITHM, stamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = signing_key(str(creds["aws_secret_access_key"]), day, region)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"{SIGNING_ALGORITHM} Credential={creds['aws_access_key_id']}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return f"https://{host}{path}", headers


def ref_for(
    desired: Target,
    model: Mapping[str, Any] | None,
    deployment: Mapping[str, Any] | None,
    payload: Mapping[str, Any],
    secrets: list[str] | None = None,
) -> Ref:
    ids = {
        NAME_KEY: model_name(desired),
        HEALTH_PATH_KEY: desired.service.container.health.path,
        DIGEST_KEY: str(payload["labels"][SPEC_LABEL]),
    }
    if model is not None:
        ids[MODEL_KEY] = str(model["id"])
        ids[URL_KEY] = endpoint_url(str(model["id"]))
    if deployment is not None:
        ids[DEPLOYMENT_KEY] = str(deployment["id"])
    if secrets:
        ids[SECRETS_KEY] = ",".join(secrets)
    return Ref(provider=desired.provider, type="baseten", service=desired.name, ids=ids)


def redact(text: str, credentials: str | None = None) -> str:
    return redact_token(redact_env(text, [API_KEY_ENV, INFERENCE_API_KEY_ENV]), credentials)


def api_key(credentials: str | None = None) -> str:
    try:
        return read_token(credentials, API_KEY_ENV)
    except CredentialError as exc:
        raise CredentialError(f"{exc}; Baseten credentials missing") from None


def authorization(value: str) -> dict[str, str]:
    return {"Authorization": f"{AUTH_SCHEME} {value}"}


def inference_api_key() -> str:
    return os.environ.get(INFERENCE_API_KEY_ENV, "").strip()


def traffic_key(credentials: str | None) -> str:
    if credentials is None and inference_api_key():
        return inference_api_key()
    return api_key(credentials)


def traffic_headers(credentials: str | None = None) -> dict[str, str]:
    try:
        return authorization(traffic_key(credentials))
    except CredentialError:
        return {}


def traffic_note(credentials: str | None) -> str:
    if credentials is None:
        return "" if inference_api_key() else f"; {DEPLOY_KEY_IN_SNAPSHOT}"
    if inference_api_key():
        return f", which also signs router traffic instead of {INFERENCE_API_KEY_ENV}"
    return ", which also signs router traffic"


def error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    if not isinstance(body, dict):
        return str(body)[:200]
    parts = [str(body[field]) for field in ("error", "detail", "message") if body.get(field)]
    return "; ".join(parts)[:400] or response.reason_phrase


def spec_of(deployment: Mapping[str, Any] | None) -> str | None:
    labels = (deployment or {}).get("labels") or {}
    value = labels.get(SPEC_LABEL)
    return str(value) if value else None


def usable(deployment: Mapping[str, Any] | None, digest: str) -> bool:
    return (
        deployment is not None
        and spec_of(deployment) == digest
        and deployment.get("status") not in UNUSABLE_STATUSES
    )


def autoscaling_drift(live: Mapping[str, Any] | None, desired: Mapping[str, Any]) -> bool:
    current = live or {}
    return any(current.get(field) != value for field, value in desired.items())


def epoch_millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class BasetenApi:
    def __init__(self, http: httpx.Client, credentials: str | None = None) -> None:
        self.http = http
        self.credentials = credentials

    def call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        response = self.http.request(
            method,
            f"{API_URL}{path}",
            json=body,
            params=params,
            headers=authorization(api_key(self.credentials)),
        )
        if missing_ok and response.status_code == 404:
            return None
        if response.is_error:
            raise BasetenApiError(
                response.status_code, redact(error_detail(response), self.credentials)
            )
        return response.json() if response.content else None

    def owned_model(self, name: str, service: str, provider: str) -> dict[str, Any] | None:
        listing = self.call("GET", "/models", params={"name": name})
        models = [m for m in listing.get("models") or [] if m.get("name") == name]
        if not models:
            return None
        if len(models) > 1:
            teams = ", ".join(sorted(str(m.get("team_name") or "unknown") for m in models))
            raise RuntimeError(
                f"Baseten has {len(models)} models named {name} (teams {teams}); "
                "multihull adopts or deletes only a unique one, so rename or remove the others"
            )
        model = models[0]
        if not self.labelled_for(str(model["id"]), service, provider):
            raise RuntimeError(
                f"Baseten model {name} ({model['id']}) has no deployment labelled "
                f"{SERVICE_LABEL}={service} and {PROVIDER_LABEL}={provider}; "
                "multihull will not adopt or delete it"
            )
        return dict(model)

    def labelled_for(self, model_id: str, service: str, provider: str) -> bool:
        return any(
            (d.get("labels") or {}).get(SERVICE_LABEL) == service
            and (d.get("labels") or {}).get(PROVIDER_LABEL) == provider
            for d in self.deployments(model_id)
        )

    def environment(self, model_id: str) -> dict[str, Any] | None:
        return self.call(
            "GET", api_path("models", model_id, "environments", ENVIRONMENT), missing_ok=True
        )

    def deployments(self, model_id: str) -> list[dict[str, Any]]:
        listing = self.call("GET", api_path("models", model_id, "deployments"))
        return list(listing.get("deployments") or [])

    def push(
        self, name: str, model_id: str | None, deployment: Mapping[str, Any], now: datetime
    ) -> dict[str, Any]:
        target = {"model_id": model_id} if model_id else {"name": name}
        prepared = self.call("POST", "/prepare_model_upload", {**target, "deployment": deployment})
        if not prepared.get("creds") or not prepared.get("s3_key"):
            raise RuntimeError(f"Baseten issued no upload target for {name}")
        self.upload(prepared, truss_archive(str(deployment["raw_config"])), now)
        source = {"kind": "model_archive", "s3_key": prepared["s3_key"], "deployment": deployment}
        if model_id:
            return dict(
                self.call("POST", api_path("models", model_id, "deployments"), {"source": source})
            )
        return dict(self.call("POST", "/models", {"source": {**source, "name": name}}))

    def upload(self, prepared: Mapping[str, Any], archive: bytes, now: datetime) -> None:
        url, headers = signed_s3_put(prepared, archive, now)
        response = self.http.put(
            url, content=archive, headers=headers, timeout=UPLOAD_TIMEOUT_SECONDS
        )
        if response.is_error:
            code = S3_ERROR_CODE.search(response.text)
            reason = code.group(1) if code else response.reason_phrase
            raise BasetenApiError(response.status_code, f"archive upload refused by S3: {reason}")

    def promote(
        self, model_id: str, deployment_id: str, autoscaling: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = {
            "scale_down_previous_production": True,
            "preserve_env_instance_type": False,
            "environment_settings_update": {"autoscaling_settings": dict(autoscaling)},
        }
        return dict(
            self.call(
                "POST", api_path("models", model_id, "deployments", deployment_id, "promote"), body
            )
        )

    def set_autoscaling(self, model_id: str, settings: Mapping[str, Any]) -> None:
        self.call(
            "PATCH",
            api_path("models", model_id, "environments", ENVIRONMENT),
            {"autoscaling_settings": dict(settings)},
        )

    def logs(
        self, model_id: str, deployment_id: str, start: datetime, end: datetime
    ) -> Iterator[dict[str, Any]]:
        cursor = epoch_millis(start)
        stop = epoch_millis(end)
        while cursor <= stop:
            params = {
                "start_epoch_millis": cursor,
                "end_epoch_millis": stop,
                "direction": "asc",
                "limit": LOG_LIMIT,
            }
            page = self.call(
                "GET",
                api_path("models", model_id, "deployments", deployment_id, "logs"),
                params=params,
            )
            lines = list(page.get("logs") or [])
            yield from lines
            if len(lines) < LOG_LIMIT:
                return
            cursor = int(lines[-1]["timestamp"]) // 1_000_000 + 1


class BasetenProvider:
    type: ClassVar = "baseten"
    native_credential_env: ClassVar[tuple[str, ...]] = (API_KEY_ENV, INFERENCE_API_KEY_ENV)

    @staticmethod
    def native_credential_files() -> list[Path]:
        return []

    def __init__(
        self,
        dry_run: bool = True,
        provider_name: str = "baseten",
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        credentials: str | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.provider_name = provider_name
        self._http = http_client
        self.clock = clock or (lambda: datetime.now(UTC))
        self.credentials = credentials

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    @property
    def api(self) -> BasetenApi:
        return BasetenApi(self.http, self.credentials)

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_payload(desired),
            notes=plan_notes(desired),
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        for problem in refusals(desired):
            raise ValueError(problem)
        payload = render_deployment(desired)
        if self.dry_run:
            return ref_for(desired, None, None, payload)
        name = model_name(desired)
        model = self.current_model(observed, desired)
        secrets = self.sync_secrets(desired)
        if model is None:
            created = self.api.push(name, None, payload, self.clock())
            return ref_for(desired, created["model"], created["deployment"], payload, secrets)
        deployment = self.converge(str(model["id"]), name, payload)
        return ref_for(desired, model, deployment, payload, secrets)

    def sync_secrets(self, desired: Target) -> list[str]:
        names = desired.service.container.secrets
        if not names:
            return []
        values = resolve_secret_values(names)
        stored: list[str] = []
        for name in names:
            store_name = secret_store_name(desired, name)
            self.api.call("POST", "/secrets", {"name": store_name, "value": values[name]})
            stored.append(store_name)
        return stored

    def current_model(self, observed: Ref | None, desired: Target) -> dict[str, Any] | None:
        model_id = observed.ids.get(MODEL_KEY) if observed else None
        if model_id:
            found = self.api.call("GET", api_path("models", model_id), missing_ok=True)
            if found is not None:
                return dict(found)
        return self.api.owned_model(model_name(desired), desired.name, desired.provider)

    def converge(self, model_id: str, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        digest = str(payload["labels"][SPEC_LABEL])
        autoscaling = payload["autoscaling_settings"]
        environment = self.api.environment(model_id) or {}
        current = environment.get("current_deployment")
        candidate = environment.get("candidate_deployment")
        for deployment in (current, candidate):
            if usable(deployment, digest):
                if autoscaling_drift(environment.get("autoscaling_settings"), autoscaling):
                    self.api.set_autoscaling(model_id, autoscaling)
                return dict(deployment)
        existing = next((d for d in self.api.deployments(model_id) if usable(d, digest)), None)
        if existing is not None:
            return self.api.promote(model_id, str(existing["id"]), autoscaling)
        return dict(self.api.push(name, model_id, payload, self.clock())["deployment"])

    def model_id_for(self, ref: Ref) -> str | None:
        model_id = ref.ids.get(MODEL_KEY)
        if model_id or not ref.ids.get(NAME_KEY):
            return model_id
        found = self.api.owned_model(ref.ids[NAME_KEY], ref.service, ref.provider)
        return str(found["id"]) if found else None

    def destroy(self, ref: Ref) -> None:
        if self.dry_run:
            return
        mirrored = mirrored_names(ref.ids.get(SECRETS_KEY, ""))
        model_id = self.model_id_for(ref)
        if model_id:
            self.api.call("DELETE", api_path("models", model_id), missing_ok=True)
        for name in mirrored:
            self.api.call("DELETE", api_path("secrets", name), missing_ok=True)

    def status(self, ref: Ref) -> Observed:
        if self.dry_run:
            return Observed(phase="Unknown", message="dry run")
        model_id = ref.ids.get(MODEL_KEY)
        if not model_id:
            return Observed(phase="Unknown", message="no Baseten model id in state")
        try:
            environment = self.api.environment(model_id)
            if environment is None:
                return Observed(phase="Failed", message=f"Baseten model {model_id} not found")
            return self.observe(ref, model_id, environment)
        except (httpx.HTTPError, RuntimeError) as exc:
            return Observed(phase="Unknown", message=redact(str(exc), self.credentials))

    def observe(self, ref: Ref, model_id: str, environment: Mapping[str, Any]) -> Observed:
        desired = int((environment.get("autoscaling_settings") or {}).get("min_replica", 0))
        current = environment.get("current_deployment") or {}
        wanted = ref.ids.get(DEPLOYMENT_KEY)
        serving_ours = bool(current) and (
            str(current.get("id")) == wanted or spec_of(current) == ref.ids.get(DIGEST_KEY)
        )
        if not serving_ours:
            return self.promoting(ref, model_id, wanted, current, desired)
        return self.serving(ref, model_id, current, desired)

    def serving(
        self, ref: Ref, model_id: str, current: Mapping[str, Any], desired: int
    ) -> Observed:
        if current.get("status") != ACTIVE:
            return Observed(
                "Pending", 0, desired, f"deployment {current['id']} {current.get('status')}"
            )
        ready = int(current.get("active_replica_count") or 0)
        if not ready:
            return Observed(
                "Pending", 0, desired, f"deployment {current['id']} has no active replica"
            )
        health_path = ref.ids.get(HEALTH_PATH_KEY, DEFAULT_HEALTH_PATH)
        url = ref.ids.get(URL_KEY) or endpoint_url(model_id)
        return self.probe(f"{url.rstrip('/')}{health_path}", ready, desired)

    def promoting(
        self,
        ref: Ref,
        model_id: str,
        wanted: str | None,
        current: Mapping[str, Any],
        desired: int,
    ) -> Observed:
        serving = f"production serves {current['id']}" if current else "production serves nothing"
        if not wanted:
            return Observed("Pending", 0, desired, serving)
        deployment = self.api.call(
            "GET", api_path("models", model_id, "deployments", wanted), missing_ok=True
        )
        status = "not found" if deployment is None else str(deployment.get("status"))
        if deployment is not None and status not in UNUSABLE_STATUSES:
            return Observed("Pending", 0, desired, f"deployment {wanted} {status}; {serving}")
        if not current:
            return Observed("Failed", 0, desired, f"deployment {wanted} {status}; {serving}")
        previous = self.serving(ref, model_id, current, desired)
        message = (
            f"rollout failed: deployment {wanted} {status}; "
            f"serving the previous config from {current['id']}"
        )
        if previous.phase == "Ready":
            return Observed("Degraded", previous.ready_replicas, desired, message)
        return Observed(previous.phase, 0, desired, f"{message}, {previous.message}")

    def probe(self, url: str, ready: int, desired: int) -> Observed:
        try:
            response = self.http.get(
                url, headers=traffic_headers(self.credentials), timeout=HEALTH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            return Observed(
                "Pending",
                0,
                desired,
                f"{ready} replica(s) active, health probe {type(exc).__name__}",
            )
        if response.is_success:
            return Observed("Ready", ready, desired, f"health {response.status_code}")
        return Observed(
            "Pending",
            0,
            desired,
            f"{ready} replica(s) active, health returned {response.status_code}",
        )

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.dry_run:
            return
        model_id = ref.ids.get(MODEL_KEY)
        if not model_id:
            raise ScaleRefused("no Baseten model id in state")
        settings = {"min_replica": min, "max_replica": max}
        try:
            self.api.set_autoscaling(model_id, settings)
        except BasetenApiError as exc:
            if exc.status in PERMANENT_SCALE_STATUSES:
                raise ScaleRefused(str(exc)) from None
            raise

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        if self.dry_run:
            return
        model_id = ref.ids.get(MODEL_KEY)
        deployment_id = ref.ids.get(DEPLOYMENT_KEY)
        if not model_id or not deployment_id:
            return
        end = self.clock()
        start = end - min(since, LOG_WINDOW)
        for line in self.api.logs(model_id, deployment_id, start, end):
            yield f"{line.get('replica') or '-'} {line.get('message', '')}"

    def endpoint(self, ref: Ref) -> Endpoint:
        url = ref.ids.get(URL_KEY) or endpoint_url(ref.ids.get(MODEL_KEY, "{modelId}"))
        return Endpoint(url=url, inject_headers=traffic_headers(self.credentials))

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in BASETEN_ACCELERATOR]

    def credentials_health(self) -> CredHealth:
        present = credential_health(self.credentials, API_KEY_ENV, self.type)
        if not present.ok:
            return present
        origin = token_origin(self.credentials, API_KEY_ENV)
        try:
            user = self.api.call("GET", "/users/me")
        except BasetenApiError as exc:
            if exc.status == 401:
                return CredHealth(ok=False, message=f"Baseten rejected {origin}: {exc}")
            if exc.status == 403:
                return CredHealth(ok=False, message=f"{origin} lacks management access: {exc}")
            return CredHealth(ok=False, message=f"could not verify {origin}: {exc}")
        except httpx.HTTPError as exc:
            reason = f"could not reach Baseten: {type(exc).__name__}: {exc}"
            return CredHealth(ok=False, message=redact(reason, self.credentials))
        identity = str((user or {}).get("email") or (user or {}).get("name") or "") or None
        message = f"Baseten accepted {origin}{traffic_note(self.credentials)}"
        return CredHealth(ok=True, message=message, identity=identity)

    def rediscover(self, service: str) -> Ref | None:
        if self.dry_run:
            return None
        name = f"multihull-{service}-{self.provider_name}"
        model = self.api.owned_model(name, service, self.provider_name)
        if model is None:
            return None
        model_id = str(model["id"])
        environment = self.api.environment(model_id) or {}
        deployment = environment.get("current_deployment") or environment.get(
            "candidate_deployment"
        )
        labels = (deployment or {}).get("labels") or {}
        ids = {
            MODEL_KEY: model_id,
            NAME_KEY: name,
            URL_KEY: endpoint_url(model_id),
            HEALTH_PATH_KEY: str(labels.get(HEALTH_PATH_LABEL) or DEFAULT_HEALTH_PATH),
        }
        if deployment:
            ids[DEPLOYMENT_KEY] = str(deployment["id"])
        stored = [store_name(service, self.provider_name, raw) for raw in labelled_names(labels)]
        if stored:
            ids[SECRETS_KEY] = ",".join(stored)
        return Ref(provider=self.provider_name, type="baseten", service=service, ids=ids)
