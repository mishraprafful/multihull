from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping
from typing import Any

import httpx
import yaml

from multihull.spec import ServiceSpec

API_HOST = "api.baseten.co"
TRAFFIC_PREFIX = "model-"
TRAFFIC_SUFFIX = ".api.baseten.co"
SYNC_PREFIX = "/environments/production/sync/"
UPLOAD_BUCKET = "baseten-fake-uploads"
UPLOAD_REGION = "us-west-2"
UPLOAD_HOST = f"{UPLOAD_BUCKET}.s3.{UPLOAD_REGION}.amazonaws.com"
AUTOSCALING_FIELDS = {
    "min_replica",
    "max_replica",
    "autoscaling_window",
    "scale_down_delay",
    "concurrency_target",
    "target_utilization_percentage",
    "target_in_flight_tokens",
    "max_scale_down_rate",
}
DEPLOYMENT_FIELDS = {
    "config",
    "raw_config",
    "user_env",
    "environment_name",
    "create_environment_if_missing",
    "region",
    "preserve_env_instance_type",
    "deploy_timeout_minutes",
    "deployment_name",
    "labels",
    "is_development",
    "autoscaling_settings",
    "request_backpressure_settings",
    "autoscaling_schedule_settings",
    "promotion_settings_override",
    "instance_type_id",
}
DOCKER_SERVER_REQUIRED = {
    "server_port",
    "predict_endpoint",
    "readiness_endpoint",
    "liveness_endpoint",
}
DEFAULT_AUTOSCALING = {
    "min_replica": 0,
    "max_replica": 1,
    "autoscaling_window": 60,
    "scale_down_delay": 900,
    "concurrency_target": 1,
    "target_utilization_percentage": 70,
}


BASETEN_TARGET = {"provider": "baseten-us", "type": "baseten", "priority": 4, "baseten": {}}


def with_baseten(raw: dict[str, Any]) -> ServiceSpec:
    return ServiceSpec.model_validate({**raw, "targets": [*raw["targets"], BASETEN_TARGET]})


def error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": message})


class FakeBaseten:
    def __init__(self, caller: str) -> None:
        self.caller = caller
        self.models: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, dict[str, Any]] = {}
        self.environments: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, str] = {}
        self.issued: dict[str, str] = {}
        self.uploads: dict[str, bytes] = {}
        self.health: dict[str, int] = {}
        self.log_lines: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.failure: httpx.Response | None = None
        self.promote_on_create = True
        self.upload_refusal: str | None = None
        self.counter = 0
        self.upload_key_id = "F" * 20
        self.upload_session = "s" * 48

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        host = request.url.host
        if host == UPLOAD_HOST:
            return self.upload(request)
        if request.headers.get("authorization") != f"Api-Key {self.caller}":
            return error(401, "Unauthorized")
        if self.failure is not None:
            return self.failure
        if host.startswith(TRAFFIC_PREFIX) and host.endswith(TRAFFIC_SUFFIX):
            return self.traffic(
                request, host.removeprefix(TRAFFIC_PREFIX).removesuffix(TRAFFIC_SUFFIX)
            )
        if host != API_HOST or not request.url.path.startswith("/v1/"):
            return error(404, "unknown host or path")
        parts = request.url.path.removeprefix("/v1/").split("/")
        body = json.loads(request.content) if request.content else None
        return self.route(request, parts, body)

    def route(
        self, request: httpx.Request, parts: list[str], body: dict[str, Any] | None
    ) -> httpx.Response:
        method = request.method
        if parts == ["users", "me"]:
            return httpx.Response(200, json={"id": "u1", "email": "owner@example.com"})
        if parts == ["prepare_model_upload"] and method == "POST":
            return self.prepare(body or {})
        if parts[0] == "secrets":
            return self.secret(method, parts[1:], body)
        if parts[0] == "models":
            return self.model_route(request, parts[1:], body)
        return error(404, "resource not found")

    def calls_of(self, method: str) -> list[str]:
        return [path for verb, path in self.calls if verb == method]

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:04d}"

    def model_route(
        self, request: httpx.Request, parts: list[str], body: dict[str, Any] | None
    ) -> httpx.Response:
        method = request.method
        if not parts:
            if method == "POST":
                return self.create(None, body or {})
            wanted = request.url.params.get("name")
            listed = [m for m in self.models.values() if wanted is None or m["name"] == wanted]
            return httpx.Response(200, json={"models": listed})
        model = self.models.get(parts[0])
        if model is None:
            return error(404, "model not found")
        rest = parts[1:]
        if not rest:
            if method == "DELETE":
                self.delete_model(model["id"])
                return httpx.Response(200, json={"id": model["id"], "deleted": True})
            return httpx.Response(200, json=model)
        if rest == ["environments", "production"]:
            if method == "PATCH":
                return self.patch_environment(model["id"], body or {})
            return httpx.Response(200, json=self.environment_view(model["id"]))
        if rest[0] == "deployments":
            return self.deployment_route(method, model["id"], rest[1:], request, body)
        return error(404, "resource not found")

    def deployment_route(
        self,
        method: str,
        model_id: str,
        rest: list[str],
        request: httpx.Request,
        body: dict[str, Any] | None,
    ) -> httpx.Response:
        if not rest:
            if method == "POST":
                return self.create(model_id, body or {})
            listed = [d for d in self.deployments.values() if d["model_id"] == model_id]
            return httpx.Response(200, json={"deployments": listed})
        deployment = self.deployments.get(rest[0])
        if deployment is None or deployment["model_id"] != model_id:
            return error(404, "deployment not found")
        if rest[1:] == []:
            return httpx.Response(200, json=deployment)
        if rest[1:] == ["promote"] and method == "POST":
            return self.promote(deployment, body or {})
        if rest[1:] == ["autoscaling_settings"] and method == "PATCH":
            unknown = sorted(set(body or {}) - AUTOSCALING_FIELDS)
            if unknown:
                return error(422, f"unknown autoscaling fields {unknown}")
            deployment["autoscaling_settings"].update(body or {})
            return httpx.Response(200, json={"status": "ACCEPTED", "message": "accepted"})
        if rest[1:] == ["logs"]:
            return self.logs(deployment["id"], request)
        return error(405, f"{method} not allowed")

    def prepare(self, body: Mapping[str, Any]) -> httpx.Response:
        if ("name" in body) == ("model_id" in body):
            return error(400, "exactly one of name or model_id is required")
        problem = self.invalid_deployment(body.get("deployment") or {})
        if problem:
            return error(422, problem)
        key = f"uploads/{self.next_id('up')}/truss.tgz"
        self.issued[key] = "pending"
        creds = {
            "aws_access_key_id": self.upload_key_id,
            "aws_secret_access_key": "x" * 40,
            "aws_session_token": self.upload_session,
        }
        return httpx.Response(
            200,
            json={
                "creds": creds,
                "s3_bucket": UPLOAD_BUCKET,
                "s3_key": key,
                "s3_region": UPLOAD_REGION,
            },
        )

    def invalid_deployment(self, deployment: Mapping[str, Any]) -> str | None:
        unknown = sorted(set(deployment) - DEPLOYMENT_FIELDS)
        if unknown:
            return f"unknown deployment fields {unknown}"
        unknown_scaling = sorted(
            set(deployment.get("autoscaling_settings") or {}) - AUTOSCALING_FIELDS
        )
        if unknown_scaling:
            return f"unknown autoscaling fields {unknown_scaling}"
        server = (deployment.get("config") or {}).get("docker_server") or {}
        missing = sorted(DOCKER_SERVER_REQUIRED - set(server))
        if missing or not server.get("start_command"):
            return f"docker_server needs start_command and {sorted(DOCKER_SERVER_REQUIRED)}"
        return None

    def upload(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.removeprefix("/")
        authorization = request.headers.get("authorization", "")
        signed = authorization.startswith(f"AWS4-HMAC-SHA256 Credential={self.upload_key_id}/")
        token = request.headers.get("x-amz-security-token") == self.upload_session
        if self.upload_refusal is not None:
            return httpx.Response(400, text=self.upload_refusal)
        allowed = signed and token and key in self.issued
        if request.method != "PUT" or not allowed:
            return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
        self.uploads[key] = request.content
        return httpx.Response(200)

    def archived_config(self, key: str) -> dict[str, Any]:
        with tarfile.open(fileobj=io.BytesIO(self.uploads[key]), mode="r:gz") as archive:
            assert archive.getnames() == ["config.yaml"]
            member = archive.extractfile("config.yaml")
            assert member is not None
            return dict(yaml.safe_load(member.read()))

    def create(self, model_id: str | None, body: Mapping[str, Any]) -> httpx.Response:
        source = body.get("source") or {}
        if source.get("kind") != "model_archive":
            return error(422, "source.kind must be model_archive")
        if source.get("s3_key") not in self.uploads:
            return error(400, "archive was not uploaded")
        deployment_body = source.get("deployment") or {}
        problem = self.invalid_deployment(deployment_body)
        if problem:
            return error(422, problem)
        if model_id is None:
            if not source.get("name"):
                return error(422, "name is required")
            model_id = self.new_model(source["name"], "default")
        environment = self.environments[model_id]
        settings = {
            **environment["autoscaling_settings"],
            **(deployment_body.get("autoscaling_settings") or {}),
        }
        deployment_id = self.next_id("d")
        self.deployments[deployment_id] = {
            "id": deployment_id,
            "model_id": model_id,
            "name": f"deployment-{deployment_id}",
            "status": "BUILDING",
            "is_production": False,
            "is_development": False,
            "active_replica_count": 0,
            "autoscaling_settings": dict(settings),
            "environment": None,
            "labels": dict(deployment_body.get("labels") or {}),
            "config": deployment_body.get("config"),
            "s3_key": source["s3_key"],
        }
        if deployment_body.get("environment_name") == "production":
            environment["candidate"] = deployment_id
            environment["pending_settings"] = settings
            if self.promote_on_create:
                self.finish_promotion(model_id)
        model = self.models[model_id]
        return httpx.Response(
            200, json={"model": model, "deployment": self.deployments[deployment_id]}
        )

    def new_model(self, name: str, team: str) -> str:
        model_id = self.next_id("m")
        self.models[model_id] = {
            "id": model_id,
            "name": name,
            "team_name": team,
            "created_at": "2026-10-26T12:00:00Z",
            "production_deployment_id": None,
        }
        self.environments[model_id] = {
            "autoscaling_settings": dict(DEFAULT_AUTOSCALING),
            "current": None,
            "candidate": None,
        }
        return model_id

    def add_foreign_model(self, name: str, team: str, labels: Mapping[str, str]) -> str:
        model_id = self.new_model(name, team)
        deployment_id = self.next_id("d")
        self.deployments[deployment_id] = {
            "id": deployment_id,
            "model_id": model_id,
            "status": "ACTIVE",
            "active_replica_count": 1,
            "autoscaling_settings": dict(DEFAULT_AUTOSCALING),
            "labels": dict(labels),
        }
        self.environments[model_id]["current"] = deployment_id
        return model_id

    def finish_promotion(self, model_id: str) -> None:
        environment = self.environments[model_id]
        deployment_id = environment["candidate"]
        if deployment_id is None:
            return
        settings = environment.pop("pending_settings", environment["autoscaling_settings"])
        previous = environment["current"]
        if previous and previous in self.deployments:
            self.deployments[previous].update(is_production=False, environment=None)
            self.deployments[previous]["active_replica_count"] = 0
        deployment = self.deployments[deployment_id]
        deployment.update(
            status="ACTIVE",
            is_production=True,
            environment="production",
            active_replica_count=max(1, int(settings["min_replica"])),
            autoscaling_settings=dict(settings),
        )
        environment.update(
            current=deployment_id, candidate=None, autoscaling_settings=dict(settings)
        )
        self.models[model_id]["production_deployment_id"] = deployment_id

    def promote(self, deployment: dict[str, Any], body: Mapping[str, Any]) -> httpx.Response:
        environment = self.environments[deployment["model_id"]]
        update = (body.get("environment_settings_update") or {}).get("autoscaling_settings") or {}
        environment["candidate"] = deployment["id"]
        environment["pending_settings"] = {**environment["autoscaling_settings"], **update}
        if self.promote_on_create:
            self.finish_promotion(deployment["model_id"])
        return httpx.Response(200, json=deployment)

    def environment_view(self, model_id: str) -> dict[str, Any]:
        environment = self.environments[model_id]
        current = environment["current"]
        candidate = environment["candidate"]
        return {
            "name": "production",
            "model_id": model_id,
            "current_deployment": self.deployments.get(current) if current else None,
            "candidate_deployment": self.deployments.get(candidate) if candidate else None,
            "autoscaling_settings": dict(environment["autoscaling_settings"]),
        }

    def patch_environment(self, model_id: str, body: Mapping[str, Any]) -> httpx.Response:
        settings = body.get("autoscaling_settings") or {}
        unknown = sorted(set(settings) - AUTOSCALING_FIELDS)
        if unknown:
            return error(422, f"unknown autoscaling fields {unknown}")
        if int(settings.get("min_replica", 0)) > int(
            settings.get(
                "max_replica", self.environments[model_id]["autoscaling_settings"]["max_replica"]
            )
        ):
            return error(400, "min_replica exceeds max_replica")
        self.environments[model_id]["autoscaling_settings"].update(settings)
        current = self.environments[model_id]["current"]
        if current:
            self.deployments[current]["autoscaling_settings"].update(settings)
        return httpx.Response(
            200,
            json={
                "environment": self.environment_view(model_id),
                "status": "ACCEPTED",
                "message": "accepted",
            },
        )

    def delete_model(self, model_id: str) -> None:
        del self.models[model_id]
        self.environments.pop(model_id, None)
        for deployment_id in [d for d, v in self.deployments.items() if v["model_id"] == model_id]:
            del self.deployments[deployment_id]

    def traffic(self, request: httpx.Request, model_id: str) -> httpx.Response:
        if model_id not in self.models:
            return error(404, "model does not exist")
        if not request.url.path.startswith(SYNC_PREFIX):
            return error(404, "unknown route")
        return httpx.Response(self.health.get(model_id, 200))

    def logs(self, deployment_id: str, request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start_epoch_millis"]) * 1_000_000
        end = int(request.url.params["end_epoch_millis"]) * 1_000_000
        limit = int(request.url.params.get("limit", "500"))
        lines = [
            line
            for line in self.log_lines.get(deployment_id, [])
            if start <= int(line["timestamp"]) <= end
        ]
        return httpx.Response(200, json={"logs": lines[:limit]})

    def secret(self, method: str, parts: list[str], body: dict[str, Any] | None) -> httpx.Response:
        if not parts and method == "GET":
            return httpx.Response(200, json={"secrets": [{"name": name} for name in self.secrets]})
        if not parts and method == "POST":
            assert body is not None
            self.secrets[body["name"]] = body["value"]
            return httpx.Response(200, json={"id": body["name"], "name": body["name"]})
        if len(parts) == 1 and method == "DELETE":
            if parts[0] not in self.secrets:
                return error(404, "secret not found")
            del self.secrets[parts[0]]
            return httpx.Response(200, json={"name": parts[0]})
        return error(405, method)
