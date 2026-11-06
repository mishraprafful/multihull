from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SPECS_DIR = Path(__file__).resolve().parents[1] / "tests" / "specs"
README = SPECS_DIR / "README.md"
SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "baseten": (
        "https://api.baseten.co/v1/spec",
        ("/v1/models", "/v1/prepare_model_upload", "/v1/secrets", "/v1/users/me"),
    ),
    "replicate": (
        "https://api.replicate.com/openapi.json",
        ("/account", "/deployments", "/hardware", "/models"),
    ),
    "runpod": (
        "https://api.runpod.io/v2/openapi.json",
        ("/v2/account/secrets", "/v2/serverless"),
    ),
}
TOP_LEVEL = ("openapi", "info", "jsonSchemaDialect", "servers", "security")
COMPONENT_PREFIX = "#/components/"


def references(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from references(value)
    elif isinstance(node, list):
        for item in node:
            yield from references(item)


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def kept_path(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def prune(spec: dict[str, Any], prefixes: tuple[str, ...]) -> dict[str, Any]:
    paths = {path: item for path, item in spec["paths"].items() if kept_path(path, prefixes)}
    available = spec.get("components") or {}
    components: dict[str, dict[str, Any]] = {}
    pending = list(references(paths))
    while pending:
        ref = pending.pop()
        if not ref.startswith(COMPONENT_PREFIX):
            raise ValueError(f"unsupported reference {ref}")
        kind, name = (unescape(token) for token in ref.removeprefix(COMPONENT_PREFIX).split("/"))
        if name in components.get(kind, {}):
            continue
        component = available[kind][name]
        components.setdefault(kind, {})[name] = component
        pending.extend(references(component))
    if "securitySchemes" in available:
        components["securitySchemes"] = available["securitySchemes"]
    pruned = {field: spec[field] for field in TOP_LEVEL if field in spec}
    pruned["paths"] = dict(sorted(paths.items()))
    pruned["components"] = {
        kind: dict(sorted(entries.items())) for kind, entries in sorted(components.items())
    }
    return pruned


def stamp_readme(provider: str, day: str) -> None:
    row = re.compile(rf"^(\| {provider} \| [^|]+ \| )\d{{4}}-\d{{2}}-\d{{2}}( \|)", re.MULTILINE)
    text = README.read_text()
    updated, count = row.subn(rf"\g<1>{day}\g<2>", text)
    if count != 1:
        raise ValueError(f"{README} has no row for {provider}")
    README.write_text(updated)


def main(selected: list[str]) -> int:
    day = datetime.now(UTC).date().isoformat()
    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        for provider in selected or sorted(SOURCES):
            url, prefixes = SOURCES[provider]
            response = http.get(url)
            response.raise_for_status()
            pruned = prune(response.json(), prefixes)
            target = SPECS_DIR / f"{provider}.openapi.json"
            target.write_text(json.dumps(pruned, indent=2, ensure_ascii=False) + "\n")
            stamp_readme(provider, day)
            print(f"{provider}: {len(pruned['paths'])} paths from {url} -> {target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
