from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from multihull.providers.replicate import CogCli

API_HOST = "api.replicate.com"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "replicate-echo"
OFFLINE_GRACE = timedelta(minutes=15)
HARDWARE = (
    {"name": "CPU", "sku": "cpu"},
    {"name": "Nvidia T4 GPU", "sku": "gpu-t4"},
    {"name": "Nvidia L40S GPU", "sku": "gpu-l40s"},
    {"name": "Nvidia A100 (80GB) GPU", "sku": "gpu-a100-large"},
    {"name": "Nvidia H100 GPU", "sku": "gpu-h100"},
)
MODEL_CREATE_FIELDS = {"owner", "name", "visibility", "hardware", "description"}
DEPLOYMENT_CREATE_FIELDS = {
    "name",
    "model",
    "version",
    "hardware",
    "min_instances",
    "max_instances",
}
DEPLOYMENT_UPDATE_FIELDS = {"version", "hardware", "min_instances", "max_instances"}
INSTANCE_LIMITS = {"min_instances": 5, "max_instances": 20}


def problem(status: int, title: str, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"title": title, "status": status, "detail": detail},
        headers={"content-type": "application/problem+json"},
    )


@dataclass
class Deployment:
    owner: str
    name: str
    model: str
    releases: list[dict[str, Any]] = field(default_factory=list)
    offline_since: datetime | None = None
    last_used: datetime | None = None

    def view(self) -> dict[str, Any]:
        return {"owner": self.owner, "name": self.name, "current_release": self.releases[-1]}


class FakeReplicate:
    def __init__(self, caller: str, owner: str = "acme", grace: timedelta = OFFLINE_GRACE) -> None:
        self.caller = caller
        self.owner = owner
        self.grace = grace
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        self.models: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, list[str]] = {}
        self.deployments: dict[str, Deployment] = {}
        self.hardware = [dict(item) for item in HARDWARE]
        self.calls: list[tuple[str, str]] = []
        self.page_size = 100
        self.next_base = f"https://{API_HOST}"
        self.failure: httpx.Response | None = None
        self.delete_failure: httpx.Response | None = None

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def calls_of(self, method: str) -> list[str]:
        return [path for verb, path in self.calls if verb == method]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.headers.get("authorization") != f"Bearer {self.caller}":
            return problem(401, "Unauthenticated", "You did not pass a valid authentication token")
        if self.failure is not None:
            return self.failure
        if request.url.host != API_HOST or not request.url.path.startswith("/v1/"):
            return problem(404, "Not found", "unknown host or path")
        parts = request.url.path.removeprefix("/v1/").split("/")
        body = json.loads(request.content) if request.content else None
        method = request.method
        if parts == ["account"] and method == "GET":
            return httpx.Response(200, json={"type": "user", "username": self.owner})
        if parts == ["hardware"] and method == "GET":
            return httpx.Response(200, json=self.hardware)
        if parts[0] == "models":
            return self.model_route(method, parts[1:], body or {})
        if parts[0] == "deployments":
            return self.deployment_route(request, parts[1:], body or {})
        return problem(404, "Not found", "resource not found")

    def model_route(self, method: str, parts: list[str], body: Mapping[str, Any]) -> httpx.Response:
        if not parts and method == "POST":
            unknown = sorted(set(body) - MODEL_CREATE_FIELDS)
            if unknown:
                return problem(400, "Bad request", f"unknown fields {unknown}")
            if body.get("owner") != self.owner:
                return problem(403, "Forbidden", "owner must be the token's account")
            key = f"{body['owner']}/{body['name']}"
            if key in self.models:
                return problem(409, "Conflict", "model exists")
            self.models[key] = {**body, "latest_version": None}
            self.versions[key] = []
            return httpx.Response(201, json=self.models[key])
        if len(parts) == 2 and method == "GET":
            found = self.models.get("/".join(parts))
            if found is None:
                return problem(404, "Not found", "model not found")
            return httpx.Response(200, json=found)
        return problem(405, "Method not allowed", method)

    def push(self, owner: str, name: str) -> str:
        key = f"{owner}/{name}"
        if key not in self.models:
            raise RuntimeError(f"{key} does not exist on Replicate")
        version = hashlib.sha256(f"{key}#{len(self.versions[key])}".encode()).hexdigest()
        self.versions[key].append(version)
        self.models[key]["latest_version"] = {"id": version, "cog_version": "0.16.0"}
        return version

    def deployment_route(
        self, request: httpx.Request, parts: list[str], body: Mapping[str, Any]
    ) -> httpx.Response:
        method = request.method
        if not parts:
            if method == "POST":
                return self.create(body)
            return self.listing(request)
        key = "/".join(parts[:2])
        deployment = self.deployments.get(key)
        if deployment is None:
            return problem(404, "Not found", "deployment not found")
        if len(parts) == 2:
            if method == "GET":
                return httpx.Response(200, json=deployment.view())
            if method == "PATCH":
                return self.update(deployment, body)
            if method == "DELETE":
                return self.delete(key, deployment)
        if parts[2:] == ["predictions"] and method == "POST":
            deployment.last_used = self.now
            output = (body.get("input") or {}).get("text")
            return httpx.Response(201, json={"status": "succeeded", "output": output})
        return problem(405, "Method not allowed", method)

    def invalid(self, body: Mapping[str, Any]) -> httpx.Response | None:
        for field_name, limit in INSTANCE_LIMITS.items():
            value = body.get(field_name)
            if value is not None and not 0 <= int(value) <= limit:
                return problem(400, "Bad request", f"{field_name} must be between 0 and {limit}")
        if "hardware" in body and body["hardware"] not in {h["sku"] for h in self.hardware}:
            return problem(400, "Bad request", f"unknown hardware {body['hardware']}")
        return None

    def release(self, deployment: Deployment, settings: Mapping[str, Any]) -> None:
        configuration = {k: settings[k] for k in ("hardware", "min_instances", "max_instances")}
        deployment.releases.append(
            {
                "number": len(deployment.releases) + 1,
                "model": deployment.model,
                "version": settings["version"],
                "created_at": self.now.isoformat().replace("+00:00", "Z"),
                "configuration": configuration,
            }
        )
        if configuration["min_instances"] == 0:
            deployment.offline_since = deployment.offline_since or self.now
        else:
            deployment.offline_since = None

    def create(self, body: Mapping[str, Any]) -> httpx.Response:
        if set(body) != DEPLOYMENT_CREATE_FIELDS:
            return problem(400, "Bad request", f"fields must be {sorted(DEPLOYMENT_CREATE_FIELDS)}")
        rejected = self.invalid(body)
        if rejected is not None:
            return rejected
        if body["version"] not in self.versions.get(body["model"], []):
            return problem(400, "Bad request", "version does not belong to the model")
        key = f"{self.owner}/{body['name']}"
        if key in self.deployments:
            return problem(409, "Conflict", "deployment exists")
        deployment = Deployment(self.owner, body["name"], body["model"])
        self.release(deployment, body)
        self.deployments[key] = deployment
        return httpx.Response(200, json=deployment.view())

    def update(self, deployment: Deployment, body: Mapping[str, Any]) -> httpx.Response:
        unknown = sorted(set(body) - DEPLOYMENT_UPDATE_FIELDS)
        if unknown or not body:
            return problem(400, "Bad request", f"unknown or no fields {unknown}")
        rejected = self.invalid(body)
        if rejected is not None:
            return rejected
        current = deployment.releases[-1]
        self.release(
            deployment, {"version": current["version"], **current["configuration"], **body}
        )
        return httpx.Response(200, json=deployment.view())

    def delete(self, key: str, deployment: Deployment) -> httpx.Response:
        if self.delete_failure is not None:
            return self.delete_failure
        quiet_since = max(
            filter(None, [deployment.offline_since, deployment.last_used]), default=None
        )
        if deployment.offline_since is None or quiet_since is None:
            return problem(409, "Conflict", "deployment is online")
        if self.now - quiet_since < self.grace:
            return problem(409, "Conflict", "deployment must be offline and unused for 15 minutes")
        del self.deployments[key]
        return httpx.Response(204)

    def listing(self, request: httpx.Request) -> httpx.Response:
        everything = [d.view() for d in reversed(self.deployments.values())]
        offset = int(request.url.params.get("cursor", "0"))
        page = everything[offset : offset + self.page_size]
        more = offset + self.page_size < len(everything)
        next_url = f"{self.next_base}/v1/deployments?cursor={offset + self.page_size}"
        return httpx.Response(
            200,
            json={"next": next_url if more else None, "previous": None, "results": page},
        )

    def deployment_settings(self, name: str) -> dict[str, Any]:
        release = self.deployments[f"{self.owner}/{name}"].releases[-1]
        return {"version": release["version"], **release["configuration"]}


class FakeCog(CogCli):
    def __init__(self, api: FakeReplicate, installed: bool = True) -> None:
        super().__init__()
        self.api = api
        self.installed = installed
        self.logins: list[str] = []
        self.pushes: list[tuple[str, Path]] = []
        self.failure: str | None = None

    def available(self) -> bool:
        return self.installed

    def login(self, credential: str) -> None:
        self.logins.append(credential)

    def push(self, image: str, context: Path) -> None:
        if self.failure is not None:
            raise RuntimeError(self.failure)
        if not (context / "cog.yaml").is_file():
            raise RuntimeError(f"no cog.yaml in {context}")
        self.pushes.append((image, context))
        _, owner, name = image.split("/")
        self.api.push(owner, name)


def echo_raw() -> dict[str, Any]:
    return yaml.safe_load((EXAMPLE / "multihull.yaml").read_text())
