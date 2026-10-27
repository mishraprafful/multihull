from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

API_HOST = "api.runpod.io"
TRAFFIC_SUFFIX = ".api.runpod.ai"
CONTAINER_FIELDS = {"image", "args", "cmd", "entrypoint", "disk", "env", "ports", "registry"}
UPDATE_FIELDS = CONTAINER_FIELDS | {
    "name",
    "workers",
    "scaling",
    "gpu",
    "cpu",
    "dataCenterIds",
    "networkVolumes",
    "mounts",
    "timeout",
    "flashboot",
    "templateId",
}
CREATE_FIELDS = UPDATE_FIELDS | {"type"}
MERGED_FIELDS = {"workers", "gpu"}
WORKER_STATES = ("running", "idle", "initializing", "throttled", "unhealthy")


def problem(status: int, title: str, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"title": title, "status": status, "detail": detail},
        headers={"content-type": "application/problem+json"},
    )


class FakeRunpod:
    def __init__(self, caller: str, traffic_caller: str | None = None) -> None:
        self.caller = caller
        self.traffic_caller = traffic_caller
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, str]] = {}
        self.worker_states: dict[str, list[str]] = {}
        self.health: dict[str, int] = {}
        self.log_events: dict[str, list[dict[str, str]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.page_size: int | None = None
        self.failure: httpx.Response | None = None
        self.counter = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        host = request.url.host
        traffic = host.endswith(TRAFFIC_SUFFIX)
        expected = (self.traffic_caller or self.caller) if traffic else self.caller
        if request.headers.get("authorization") != f"Bearer {expected}":
            return problem(401, "Unauthorized", "missing bearer token")
        if self.failure is not None:
            return self.failure
        if traffic:
            return httpx.Response(self.health.get(host.removesuffix(TRAFFIC_SUFFIX), 200))
        if host != API_HOST or not request.url.path.startswith("/v2/"):
            return problem(404, "Not Found", "unknown host or path")
        parts = request.url.path.removeprefix("/v2/").split("/")
        body = json.loads(request.content) if request.content else None
        if parts[0] == "serverless":
            return self.serverless(request, parts[1:], body)
        if parts[:2] == ["account", "secrets"]:
            return self.secret(request, parts[2:], body)
        return problem(404, "Not Found", "resource not found")

    def calls_of(self, method: str) -> list[str]:
        return [path for verb, path in self.calls if verb == method]

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:04d}"

    def serverless(
        self, request: httpx.Request, parts: list[str], body: dict[str, Any] | None
    ) -> httpx.Response:
        method = request.method
        if not parts:
            if method == "POST":
                return self.create(body or {})
            return self.listing(request)
        endpoint = self.endpoints.get(parts[0])
        if endpoint is None:
            return problem(404, "Not Found", "resource not found")
        if len(parts) == 1:
            if method == "GET":
                return httpx.Response(200, json=endpoint)
            if method == "PATCH":
                return self.update(endpoint, body or {})
            if method == "DELETE":
                del self.endpoints[parts[0]]
                return httpx.Response(204)
        if parts[1:] == ["workers"]:
            return httpx.Response(200, json=self.workers(endpoint))
        if len(parts) == 4 and parts[1] == "workers" and parts[3] == "logs":
            return self.logs(parts[2])
        return problem(405, "Method Not Allowed", f"{method} {request.url.path}")

    def create(self, body: Mapping[str, Any]) -> httpx.Response:
        unknown = sorted(set(body) - CREATE_FIELDS)
        if unknown:
            return problem(422, "Unprocessable Entity", f"unknown properties {unknown}")
        if body.get("type") == "LOAD_BALANCER" and body["scaling"]["type"] != "REQUEST_COUNT":
            return problem(422, "Unprocessable Entity", "load balancers scale on REQUEST_COUNT")
        if ("gpu" in body) == ("cpu" in body):
            return problem(400, "Bad Request", "set exactly one of gpu or cpu")
        if "gpu" in body and not body["gpu"].get("pools"):
            return problem(422, "Unprocessable Entity", "gpu.pools needs at least one pool")
        endpoint_id = self.next_id("ep")
        self.endpoints[endpoint_id] = {**body, "id": endpoint_id, **self.urls(endpoint_id, body)}
        return httpx.Response(201, json=self.endpoints[endpoint_id])

    def urls(self, endpoint_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        base = f"https://{endpoint_id}{TRAFFIC_SUFFIX}"
        health = (body.get("env") or {}).get("HEALTH_CHECK_PATH", "/ping")
        return {"requestUrls": {"base": base, "health": f"{base}{health}"}}

    def update(self, endpoint: dict[str, Any], body: Mapping[str, Any]) -> httpx.Response:
        unknown = sorted(set(body) - UPDATE_FIELDS)
        if unknown:
            return problem(422, "Unprocessable Entity", f"unknown properties {unknown}")
        for field, value in body.items():
            if field in MERGED_FIELDS:
                endpoint[field] = {**endpoint.get(field, {}), **value}
            else:
                endpoint[field] = value
        endpoint.update(self.urls(endpoint["id"], endpoint))
        return httpx.Response(200, json=endpoint)

    def listing(self, request: httpx.Request) -> httpx.Response:
        newest_first = list(reversed(self.endpoints.values()))
        limit = self.page_size or int(request.url.params.get("limit", "1000"))
        offset = int(request.url.params.get("cursor", "0"))
        page = newest_first[offset : offset + limit]
        more = offset + limit < len(newest_first)
        return httpx.Response(
            200,
            json={
                "endpoints": page,
                "pagination": {
                    "nextCursor": str(offset + limit) if more else None,
                    "hasNextPage": more,
                },
            },
        )

    def workers(self, endpoint: Mapping[str, Any]) -> dict[str, Any]:
        default = ["IDLE"] * int(endpoint["workers"]["min"])
        states = self.worker_states.get(endpoint["id"], default)
        workers = [
            {"id": f"{endpoint['id']}-w{index}", "status": state}
            for index, state in enumerate(states)
        ]
        summary = {state: states.count(state.upper()) for state in WORKER_STATES}
        return {"workers": workers, "summary": {**summary, "total": len(states)}}

    def logs(self, worker_id: str) -> httpx.Response:
        frames = [
            f"id: {event['ts']}\ndata: {json.dumps(event)}\n\n"
            for event in self.log_events.get(worker_id, [])
        ]
        return httpx.Response(
            200, text="".join(frames), headers={"content-type": "text/event-stream"}
        )

    def secret(
        self, request: httpx.Request, parts: list[str], body: dict[str, Any] | None
    ) -> httpx.Response:
        method = request.method
        if not parts and method == "GET":
            wanted = request.url.params.get("name", "").casefold()
            found = [
                {"id": secret["id"], "name": secret["name"]}
                for secret in self.secrets.values()
                if not wanted or secret["name"].casefold() == wanted
            ]
            return httpx.Response(200, json={"secrets": found})
        if not parts and method == "POST":
            assert body is not None
            if any(s["name"].casefold() == body["name"].casefold() for s in self.secrets.values()):
                return problem(409, "Conflict", "secret name exists")
            secret_id = self.next_id("sec")
            self.secrets[secret_id] = {"id": secret_id, **body}
            return httpx.Response(201, json={"id": secret_id, "name": body["name"]})
        stored = self.secrets.get(parts[0]) if parts else None
        if stored is None:
            return problem(404, "Not Found", "resource not found")
        if method == "PATCH":
            assert body is not None
            stored.update(body)
            return httpx.Response(200, json={"id": stored["id"], "name": stored["name"]})
        if method == "DELETE":
            del self.secrets[parts[0]]
            return httpx.Response(204)
        return problem(405, "Method Not Allowed", method)

    def stored_values(self) -> dict[str, str]:
        return {secret["name"]: secret["value"] for secret in self.secrets.values()}
