from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from multihull.credentials import CredentialError, read_token, token_origin
from multihull.providers.base import (
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
)
from multihull.spec import GPU, RunpodBlock

API_URL = "https://api.runpod.io/v2"
API_KEY_ENV = "RUNPOD_API_KEY"
ENDPOINT_API_KEY_ENV = "RUNPOD_ENDPOINT_API_KEY"
RUNPOD_POOL: dict[GPU, str] = {
    GPU.L4: "AMPERE_24",
    GPU.A100_80: "AMPERE_80",
    GPU.H100: "ADA_80_PRO",
    GPU.H200: "HOPPER_141",
}
CPU_FLAVORS: tuple[dict[str, Any], ...] = ({"id": "cpu5c", "vcpuCount": 4},)
ENDPOINT_TYPE = "LOAD_BALANCER"
SCALER_TYPE = "REQUEST_COUNT"
CONTAINER_DISK_GB = 20
IDLE_TIMEOUT_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 15.0
HEALTH_TIMEOUT_SECONDS = 5.0
LOG_IDLE_SECONDS = 3.0
PAGE_SIZE = 1000
WARMING_STATUS = 204
PERMANENT_SCALE_STATUSES = frozenset({400, 422})
DEFAULT_HEALTH_PATH = "/health"
HEALTH_CHECK_PATH_ENV = "HEALTH_CHECK_PATH"
AUTOSCALER_FIELDS = ("workers", "scaling")
ENDPOINT_KEY = "endpoint"
NAME_KEY = "name"
URL_KEY = "url"
HEALTH_PATH_KEY = "health_path"
DIGEST_KEY = "spec"
SECRETS_KEY = "secrets"
UNSAFE_SECRET_CHARS = re.compile(r"[^A-Za-z0-9_]")
SECRET_REFERENCE = re.compile(r"\{\{ RUNPOD_SECRET_([A-Za-z0-9_]+) \}\}")


class RunpodApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"RunPod API {status}: {detail}")
        self.status = status


def runpod_block(desired: Target) -> RunpodBlock:
    return desired.target.runpod or RunpodBlock()


def endpoint_name(desired: Target) -> str:
    return f"{desired.resource_name}-{desired.provider}"


def endpoint_url(endpoint_id: str) -> str:
    return f"https://{endpoint_id}.api.runpod.ai"


def base_url(endpoint: Mapping[str, Any]) -> str:
    urls = endpoint.get("requestUrls") or {}
    return str(urls.get("base") or endpoint_url(str(endpoint["id"])))


def secret_store_name(desired: Target, secret: str) -> str:
    return UNSAFE_SECRET_CHARS.sub("_", f"{endpoint_name(desired)}_{secret}")


def secret_reference(store_name: str) -> str:
    return "{{ RUNPOD_SECRET_" + store_name + " }}"


def owned_secrets(env: Mapping[str, Any], endpoint: str) -> list[str]:
    owned: list[str] = []
    for key, value in env.items():
        match = SECRET_REFERENCE.fullmatch(str(value))
        if match and match.group(1) == UNSAFE_SECRET_CHARS.sub("_", f"{endpoint}_{key}"):
            owned.append(match.group(1))
    return owned


def gpu_pools(desired: Target) -> list[str]:
    return list(dict.fromkeys(RUNPOD_POOL[gpu] for gpu in desired.gpus if gpu in RUNPOD_POOL))


def unsupported_gpus(desired: Target) -> list[GPU]:
    return [gpu for gpu in desired.gpus if gpu not in RUNPOD_POOL]


def no_pool_message(gpus: Sequence[GPU]) -> str:
    names = ", ".join(gpu.value for gpu in gpus)
    supported = ", ".join(gpu.value for gpu in RUNPOD_POOL)
    return f"RunPod has no serverless GPU pool for {names}; resources.gpu needs one of {supported}"


def compute(desired: Target) -> dict[str, Any]:
    if not desired.gpus:
        return {"cpu": [dict(flavor) for flavor in CPU_FLAVORS]}
    return {"gpu": {"pools": gpu_pools(desired), "count": desired.service.resources.gpuCount}}


def endpoint_env(desired: Target) -> dict[str, str]:
    container = desired.service.container
    env = dict(sorted(container.env.items()))
    env.update(
        {name: secret_reference(secret_store_name(desired, name)) for name in container.secrets}
    )
    env.update(
        {
            "PORT": str(container.port),
            "PORT_HEALTH": str(container.port),
            HEALTH_CHECK_PATH_ENV: container.health.path,
        }
    )
    return env


def render_endpoint(desired: Target) -> dict[str, Any]:
    container = desired.service.container
    replicas = desired.replicas
    body: dict[str, Any] = {
        "name": endpoint_name(desired),
        "type": ENDPOINT_TYPE,
        "image": desired.image_ref,
    }
    if container.command:
        body["entrypoint"] = list(container.command)
        body["cmd"] = []
    body.update(
        {
            "ports": [f"{container.port}/http"],
            "env": endpoint_env(desired),
            "disk": CONTAINER_DISK_GB,
            **compute(desired),
            "workers": {
                "min": replicas.min,
                "max": replicas.max,
                "idleTimeout": IDLE_TIMEOUT_SECONDS,
            },
            "scaling": {
                "type": SCALER_TYPE,
                "requestCount": desired.service.scaling.concurrency or 1,
            },
            "dataCenterIds": list(runpod_block(desired).dataCenters),
        }
    )
    return body


def render_payload(desired: Target) -> dict[str, Any]:
    return {
        "secrets": [
            {"name": secret_store_name(desired, name)} for name in desired.service.container.secrets
        ],
        "endpoint": render_endpoint(desired),
    }


def plan_notes(desired: Target) -> list[str]:
    missing = unsupported_gpus(desired)
    if not missing:
        return []
    if not gpu_pools(desired):
        return [no_pool_message(missing)]
    names = ", ".join(gpu.value for gpu in missing)
    return [f"RunPod has no serverless GPU pool for {names}; those classes are skipped"]


def update_body(body: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in body.items() if field != "type"}


def body_digest(body: Mapping[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def autoscaler_drift(current: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    drift: dict[str, Any] = {}
    for field in AUTOSCALER_FIELDS:
        live = current.get(field) or {}
        if any(live.get(key) != value for key, value in body[field].items()):
            drift[field] = body[field]
    return drift


def ref_for(
    desired: Target,
    endpoint: Mapping[str, Any] | None,
    body: Mapping[str, Any],
    secrets: Sequence[str] = (),
) -> Ref:
    ids = {
        NAME_KEY: str(body["name"]),
        HEALTH_PATH_KEY: desired.service.container.health.path,
        DIGEST_KEY: body_digest(body),
    }
    if endpoint is not None:
        ids[ENDPOINT_KEY] = str(endpoint["id"])
        ids[URL_KEY] = base_url(endpoint)
    if secrets:
        ids[SECRETS_KEY] = ",".join(secrets)
    return Ref(provider=desired.provider, type="runpod", service=desired.name, ids=ids)


def redact(text: str, credentials: str | None = None) -> str:
    return redact_token(redact_env(text, [API_KEY_ENV, ENDPOINT_API_KEY_ENV]), credentials)


def api_key(credentials: str | None = None) -> str:
    try:
        return read_token(credentials, API_KEY_ENV)
    except CredentialError as exc:
        raise CredentialError(f"{exc}; RunPod credentials missing") from None


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def endpoint_api_key() -> str:
    return os.environ.get(ENDPOINT_API_KEY_ENV, "").strip()


def traffic_key(credentials: str | None) -> str:
    if credentials is None and endpoint_api_key():
        return endpoint_api_key()
    return api_key(credentials)


def traffic_headers(credentials: str | None = None) -> dict[str, str]:
    try:
        return bearer(traffic_key(credentials))
    except CredentialError:
        return {}


def traffic_note(credentials: str | None) -> str:
    if credentials is None:
        return ""
    if endpoint_api_key():
        return f", which also signs router traffic instead of {ENDPOINT_API_KEY_ENV}"
    return ", which also signs router traffic"


def problem_detail(response: httpx.Response) -> str:
    try:
        problem = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    if not isinstance(problem, dict):
        return str(problem)[:200]
    title = str(problem.get("title") or response.reason_phrase)
    detail = str(problem.get("detail") or "")
    text = f"{title}: {detail}" if detail else title
    errors = [str(error) for error in problem.get("errors") or []]
    return "; ".join([text, *errors])


def log_event(raw: str) -> dict[str, Any] | None:
    if not raw.startswith("data:"):
        return None
    try:
        event = json.loads(raw.removeprefix("data:").strip())
    except ValueError:
        return None
    return event if isinstance(event, dict) and "line" in event else None


def logged_after(event: Mapping[str, Any], until: datetime) -> bool:
    stamp = event.get("ts")
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(str(stamp)) > until
    except ValueError:
        return False


def waiting_message(summary: Mapping[str, Any]) -> str:
    counts = ", ".join(
        f"{int(summary.get(state, 0))} {state}"
        for state in ("initializing", "throttled", "unhealthy")
    )
    return f"no worker ready: {counts}"


class RunpodApi:
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
            headers=bearer(api_key(self.credentials)),
        )
        if missing_ok and response.status_code == 404:
            return None
        if response.is_error:
            raise RunpodApiError(
                response.status_code, redact(problem_detail(response), self.credentials)
            )
        return response.json() if response.content else None

    def endpoints(self) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"limit": PAGE_SIZE}
        while True:
            page = self.call("GET", "/serverless", params=params)
            yield from page.get("endpoints") or []
            pagination = page.get("pagination") or {}
            cursor = pagination.get("nextCursor")
            if not pagination.get("hasNextPage") or not cursor:
                return
            params = {"limit": PAGE_SIZE, "cursor": cursor}

    def endpoint_named(self, name: str) -> dict[str, Any] | None:
        return next((e for e in self.endpoints() if e.get("name") == name), None)

    def secret_named(self, name: str) -> dict[str, Any] | None:
        listing = self.call("GET", "/account/secrets", params={"name": name})
        return next(
            (
                secret
                for secret in listing.get("secrets") or []
                if str(secret.get("name", "")).casefold() == name.casefold()
            ),
            None,
        )

    def put_secret(self, name: str, value: str) -> None:
        existing = self.secret_named(name)
        if existing is None:
            self.call("POST", "/account/secrets", {"name": name, "value": value})
        else:
            self.call("PATCH", api_path("account", "secrets", existing["id"]), {"value": value})

    def delete_secret(self, name: str) -> None:
        existing = self.secret_named(name)
        if existing is not None:
            self.call("DELETE", api_path("account", "secrets", existing["id"]), missing_ok=True)

    def worker_logs(
        self, endpoint_id: str, worker_id: str, start: str, until: datetime
    ) -> Iterator[str]:
        url = f"{API_URL}{api_path('serverless', endpoint_id, 'workers', worker_id, 'logs')}"
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, read=LOG_IDLE_SECONDS)
        try:
            with self.http.stream(
                "GET",
                url,
                params={"since": start},
                headers=bearer(api_key(self.credentials)),
                timeout=timeout,
            ) as response:
                if response.is_error:
                    response.read()
                    raise RunpodApiError(
                        response.status_code, redact(problem_detail(response), self.credentials)
                    )
                for raw in response.iter_lines():
                    event = log_event(raw)
                    if event is None:
                        continue
                    if logged_after(event, until):
                        return
                    yield f"{worker_id} {event['line']}"
        except httpx.ReadTimeout:
            return


class RunpodProvider:
    type: ClassVar = "runpod"

    def __init__(
        self,
        dry_run: bool = True,
        provider_name: str = "runpod",
        http_client: httpx.Client | None = None,
        credentials: str | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.provider_name = provider_name
        self._http = http_client
        self.credentials = credentials

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    @property
    def api(self) -> RunpodApi:
        return RunpodApi(self.http, self.credentials)

    def plan(self, desired: Target, observed: Ref | None) -> Plan:
        return Plan(
            provider=desired.provider,
            type=self.type,
            payload=render_payload(desired),
            notes=plan_notes(desired),
        )

    def apply(self, desired: Target, observed: Ref | None) -> Ref:
        body = render_endpoint(desired)
        if desired.gpus and not gpu_pools(desired):
            raise ValueError(no_pool_message(unsupported_gpus(desired)))
        if self.dry_run:
            return ref_for(desired, None, body)
        secrets = self.sync_secrets(desired)
        current = self.current_endpoint(observed, body["name"])
        if current is None:
            current = self.api.call("POST", "/serverless", body)
        else:
            current = self.update(current, observed, body)
        return ref_for(desired, current, body, secrets)

    def sync_secrets(self, desired: Target) -> list[str]:
        names = desired.service.container.secrets
        if not names:
            return []
        values = resolve_secret_values(names)
        stored: list[str] = []
        for name in names:
            store_name = secret_store_name(desired, name)
            self.api.put_secret(store_name, values[name])
            stored.append(store_name)
        return stored

    def current_endpoint(self, observed: Ref | None, name: str) -> dict[str, Any] | None:
        endpoint_id = observed.ids.get(ENDPOINT_KEY) if observed else None
        if endpoint_id:
            found = self.api.call("GET", api_path("serverless", endpoint_id), missing_ok=True)
            if found is not None:
                return dict(found)
        return self.api.endpoint_named(name)

    def update(
        self, current: dict[str, Any], observed: Ref | None, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        if current.get("type") != ENDPOINT_TYPE:
            raise RuntimeError(
                f"RunPod endpoint {current.get('name')} ({current['id']}) is "
                f"{current.get('type')}, not {ENDPOINT_TYPE}; delete it or rename the target"
            )
        unchanged = (
            observed is not None
            and observed.ids.get(ENDPOINT_KEY) == str(current["id"])
            and observed.ids.get(DIGEST_KEY) == body_digest(body)
        )
        changes = autoscaler_drift(current, body) if unchanged else update_body(body)
        if not changes:
            return current
        return dict(self.api.call("PATCH", api_path("serverless", current["id"]), changes))

    def destroy(self, ref: Ref) -> None:
        if self.dry_run:
            return
        mirrored = mirrored_names(ref.ids.get(SECRETS_KEY, ""))
        endpoint_id = ref.ids.get(ENDPOINT_KEY)
        if not endpoint_id and ref.ids.get(NAME_KEY):
            found = self.api.endpoint_named(ref.ids[NAME_KEY])
            endpoint_id = str(found["id"]) if found else None
        if endpoint_id:
            self.api.call("DELETE", api_path("serverless", endpoint_id), missing_ok=True)
        for name in mirrored:
            self.api.delete_secret(name)

    def status(self, ref: Ref) -> Observed:
        if self.dry_run:
            return Observed(phase="Unknown", message="dry run")
        endpoint_id = ref.ids.get(ENDPOINT_KEY)
        if not endpoint_id:
            return Observed(phase="Unknown", message="no RunPod endpoint id in state")
        try:
            endpoint = self.api.call("GET", api_path("serverless", endpoint_id), missing_ok=True)
            if endpoint is None:
                return Observed(phase="Failed", message=f"RunPod endpoint {endpoint_id} not found")
            listing = self.api.call("GET", api_path("serverless", endpoint_id, "workers"))
        except (httpx.HTTPError, RuntimeError) as exc:
            return Observed(phase="Unknown", message=redact(str(exc), self.credentials))
        summary = listing.get("summary") or {}
        ready = int(summary.get("running", 0)) + int(summary.get("idle", 0))
        minimum = int((endpoint.get("workers") or {}).get("min", 0))
        desired = max(minimum, int(summary.get("total", 0)))
        if not ready:
            return Observed("Pending", 0, desired, waiting_message(summary))
        health_path = ref.ids.get(HEALTH_PATH_KEY, DEFAULT_HEALTH_PATH)
        return self.probe(f"{base_url(endpoint).rstrip('/')}{health_path}", ready, desired)

    def probe(self, url: str, ready: int, desired: int) -> Observed:
        try:
            response = self.http.get(
                url, headers=traffic_headers(self.credentials), timeout=HEALTH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            return Observed(
                "Pending", 0, desired, f"{ready} worker(s) up, health probe {type(exc).__name__}"
            )
        if response.status_code == WARMING_STATUS:
            return Observed(
                "Pending", 0, desired, f"{ready} worker(s) up, health {WARMING_STATUS} (warming)"
            )
        if response.is_success:
            return Observed("Ready", ready, desired, f"health {response.status_code}")
        return Observed(
            "Pending", 0, desired, f"{ready} worker(s) up, health returned {response.status_code}"
        )

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.dry_run:
            return
        endpoint_id = ref.ids.get(ENDPOINT_KEY)
        if not endpoint_id:
            raise ScaleRefused("no RunPod endpoint id in state")
        try:
            self.api.call(
                "PATCH", api_path("serverless", endpoint_id), {"workers": {"min": min, "max": max}}
            )
        except RunpodApiError as exc:
            if exc.status in PERMANENT_SCALE_STATUSES:
                raise ScaleRefused(str(exc)) from None
            raise

    def logs(self, ref: Ref, since: timedelta) -> Iterator[str]:
        if self.dry_run:
            return
        endpoint_id = ref.ids.get(ENDPOINT_KEY)
        if not endpoint_id:
            return
        until = datetime.now(UTC)
        start = (until - since).strftime("%Y-%m-%dT%H:%M:%SZ")
        listing = self.api.call("GET", api_path("serverless", endpoint_id, "workers"))
        for worker in listing.get("workers") or []:
            yield from self.api.worker_logs(endpoint_id, str(worker["id"]), start, until)

    def endpoint(self, ref: Ref) -> Endpoint:
        url = ref.ids.get(URL_KEY) or endpoint_url(ref.ids.get(ENDPOINT_KEY, "{endpointId}"))
        return Endpoint(
            url=url, inject_headers=traffic_headers(self.credentials), region=ref.ids.get("region")
        )

    def gpu_inventory(self) -> list[GPUOffer]:
        return [GPUOffer(gpu=gpu, available=True) for gpu in RUNPOD_POOL]

    def credentials_health(self) -> CredHealth:
        present = credential_health(self.credentials, API_KEY_ENV, self.type)
        if not present.ok:
            return present
        origin = token_origin(self.credentials, API_KEY_ENV)
        try:
            self.api.call("GET", "/serverless", params={"limit": 1})
        except RunpodApiError as exc:
            if exc.status == 401:
                return CredHealth(ok=False, message=f"RunPod rejected {origin}: {exc}")
            if exc.status == 403:
                return CredHealth(ok=False, message=f"{origin} lacks Serverless access: {exc}")
            return CredHealth(ok=False, message=f"could not verify {origin}: {exc}")
        except httpx.HTTPError as exc:
            reason = f"could not reach RunPod: {type(exc).__name__}: {exc}"
            return CredHealth(ok=False, message=redact(reason, self.credentials))
        return CredHealth(
            ok=True, message=f"RunPod accepted {origin}{traffic_note(self.credentials)}"
        )

    def rediscover(self, service: str) -> Ref | None:
        if self.dry_run:
            return None
        name = f"multihull-{service}-{self.provider_name}"
        found = self.api.endpoint_named(name)
        if found is None:
            return None
        env = found.get("env") or {}
        ids = {
            ENDPOINT_KEY: str(found["id"]),
            NAME_KEY: name,
            URL_KEY: base_url(found),
            HEALTH_PATH_KEY: str(env.get(HEALTH_CHECK_PATH_ENV) or DEFAULT_HEALTH_PATH),
        }
        stored = owned_secrets(env, name)
        if stored:
            ids[SECRETS_KEY] = ",".join(stored)
        return Ref(provider=self.provider_name, type="runpod", service=service, ids=ids)
