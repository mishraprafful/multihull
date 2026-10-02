from __future__ import annotations

import copy
from typing import Any

from docker.errors import NotFound

EPHEMERAL_PORT_BASE = 32768


class FakeContainer:
    def __init__(
        self,
        collection: FakeContainers,
        container_id: str,
        run_kwargs: dict[str, Any],
        host_port: int,
    ) -> None:
        self.collection = collection
        self.id = container_id
        self.name = run_kwargs["name"]
        self.labels: dict[str, str] = dict(run_kwargs.get("labels") or {})
        self.run_kwargs = copy.deepcopy(run_kwargs)
        self.status = "running"
        self.exit_code = 0
        self.health: str | None = None
        self.reloads = 0
        self.stopped = False
        self.started = 0
        self.log_calls: list[dict[str, Any]] = []
        self.log_chunks = [b"boot ok\nlistening on 8000\n", b"health probe"]
        self.host_port = host_port

    @property
    def attrs(self) -> dict[str, Any]:
        ports = self.run_kwargs.get("ports") or {}
        bindings = {
            key: [{"HostIp": binding[0], "HostPort": str(binding[1] or "")}]
            for key, binding in ports.items()
        }
        live = (
            {
                key: [{"HostIp": binding[0], "HostPort": str(self.host_port)}]
                for key, binding in ports.items()
            }
            if self.status == "running"
            else {}
        )
        state: dict[str, Any] = {"Status": self.status, "ExitCode": self.exit_code}
        if self.health is not None:
            state["Health"] = {"Status": self.health}
        return {
            "Id": self.id,
            "Name": f"/{self.name}",
            "State": state,
            "Config": {
                "Labels": dict(self.labels),
                "ExposedPorts": {key: {} for key in ports},
            },
            "HostConfig": {"PortBindings": bindings},
            "NetworkSettings": {"Ports": live},
        }

    def reload(self) -> None:
        self.reloads += 1

    def start(self) -> None:
        self.status = "running"
        self.started += 1

    def stop(self, timeout: int | None = None) -> None:
        self.status = "exited"
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        self.collection.items = [c for c in self.collection.items if c is not self]
        self.collection.removed.append(self.name)

    def logs(self, **kwargs: Any) -> Any:
        self.log_calls.append(kwargs)
        return iter(self.log_chunks)


class FakeContainers:
    def __init__(self) -> None:
        self.items: list[FakeContainer] = []
        self.run_calls: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.counter = 0

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(copy.deepcopy(kwargs))
        for existing in self.items:
            if existing.name == kwargs["name"]:
                raise RuntimeError(f"conflict: container {kwargs['name']} already exists")
        self.counter += 1
        requested = next(iter((kwargs.get("ports") or {}).values()), ("127.0.0.1", None))
        host_port = requested[1] or EPHEMERAL_PORT_BASE + self.counter
        container = FakeContainer(self, f"sha{self.counter:03d}", kwargs, host_port)
        self.items.append(container)
        return container

    def get(self, identifier: str) -> FakeContainer:
        for container in self.items:
            if identifier in (container.id, container.name):
                return container
        raise NotFound(f"No such container: {identifier}")

    def list(self, all: bool = False, filters: dict[str, Any] | None = None) -> list[FakeContainer]:
        wanted = {}
        for selector in (filters or {}).get("label", []):
            key, _, value = selector.partition("=")
            wanted[key] = value
        return [
            c
            for c in self.items
            if (all or c.status == "running") and all_labels_match(c.labels, wanted)
        ]


def all_labels_match(labels: dict[str, str], wanted: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in wanted.items())


class FakeImages:
    def __init__(self) -> None:
        self.pulled: list[str] = []

    def pull(self, repository: str, **kwargs: Any) -> dict[str, str]:
        self.pulled.append(repository)
        return {"Id": f"img-{repository}"}


class FakeDockerClient:
    def __init__(self, reachable: bool = True) -> None:
        self.containers = FakeContainers()
        self.images = FakeImages()
        self.reachable = reachable
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        if not self.reachable:
            raise ConnectionError("Error while fetching server API version")
        return True

    def version(self) -> dict[str, str]:
        return {"Version": "27.3.1", "ApiVersion": "1.47"}
