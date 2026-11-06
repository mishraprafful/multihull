from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

CONFIG_KEY = re.compile(r"^[-._a-zA-Z0-9]+$")
CONFIG_KEY_MESSAGE = (
    "a valid config key must consist of alphanumeric characters, '-', '_' or '.' "
    "(e.g. 'key.name',  or 'KEY_NAME',  or 'key-name', regex used for validation is "
    "'[-._a-zA-Z0-9]+')"
)
RELAXED_ENV_NAME_MESSAGE = (
    "a valid environment variable name must consist only of printable ASCII characters "
    "other than '='"
)


class FakeUnprocessableEntity(Exception):
    status = 422


def config_key_problems(key: str) -> list[str]:
    problems = []
    if len(key) > 253:
        problems.append("must be no more than 253 characters")
    if not CONFIG_KEY.match(key):
        problems.append(CONFIG_KEY_MESSAGE)
    if key == ".":
        problems.append("must not be '.'")
    elif key == "..":
        problems.append("must not be '..'")
    elif key.startswith(".."):
        problems.append("must not start with '..'")
    return problems


def relaxed_env_name_problems(name: str) -> list[str]:
    if not name:
        return ["Required value"]
    if any(not " " <= char <= "~" or char == "=" for char in name):
        return [RELAXED_ENV_NAME_MESSAGE]
    return []


def secret_key_problems(manifest: dict[str, Any]) -> list[str]:
    keys = [*manifest.get("data", {}), *manifest.get("stringData", {})]
    return [
        f'data[{key}]: Invalid value: "{key}": {problem}'
        for key in keys
        for problem in config_key_problems(key)
    ]


def env_name_problems(manifest: dict[str, Any]) -> list[str]:
    template = manifest.get("spec", {}).get("template", {})
    containers = template.get("spec", {}).get("containers", [])
    return [
        f"spec.template.spec.containers[{index}].env[{position}].name: "
        f'Invalid value: "{entry["name"]}": {problem}'
        for index, container in enumerate(containers)
        for position, entry in enumerate(container.get("env", []))
        for problem in relaxed_env_name_problems(entry["name"])
    ]


def validation_problems(manifest: dict[str, Any]) -> list[str]:
    if manifest["kind"] == "Secret":
        return secret_key_problems(manifest)
    if manifest["kind"] == "Deployment":
        return env_name_problems(manifest)
    return []


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
        problems = validation_problems(manifest)
        if problems:
            kind, name = manifest["kind"], manifest["metadata"]["name"]
            raise FakeUnprocessableEntity(f'{kind} "{name}" is invalid: ' + ", ".join(problems))
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
