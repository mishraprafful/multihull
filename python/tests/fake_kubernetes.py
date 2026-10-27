from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class FakeKubeClient:
    def __init__(self, namespaces: tuple[str, ...] = ("multihull",)) -> None:
        self.applied: list[dict[str, Any]] = []
        self.objects: dict[tuple[str, str, str | None], dict[str, Any]] = {
            ("Namespace", name, None): {"kind": "Namespace", "metadata": {"name": name}}
            for name in namespaces
        }
        self.nodes: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []

    def server_side_apply(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self.applied.append(manifest)
        key = (
            manifest["kind"],
            manifest["metadata"]["name"],
            manifest["metadata"].get("namespace"),
        )
        self.objects[key] = manifest
        return manifest

    def get(
        self, api_version: str, kind: str, name: str, namespace: str | None
    ) -> dict[str, Any] | None:
        return self.objects.get((kind, name, namespace))

    def list(
        self, api_version: str, kind: str, namespace: str | None, label_selector: str
    ) -> list[dict[str, Any]]:
        if kind == "Node":
            return self.nodes
        return [obj for (k, _, _), obj in self.objects.items() if k == kind]

    def delete(self, api_version: str, kind: str, name: str, namespace: str | None) -> None:
        self.deleted.append((kind, name))
        self.objects.pop((kind, name, namespace), None)

    def pod_logs(self, namespace: str, label_selector: str, since_seconds: int) -> Iterator[str]:
        yield f"pod-a hello {label_selector} {since_seconds}"
