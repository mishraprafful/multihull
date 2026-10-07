from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from multihull import spec as specmod
from multihull.providers.base import Target
from multihull.providers.registry import PROVIDERS
from multihull.spec import ServiceSpec
from tests.fakes import PROVIDER_TYPES, FakeProvider

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


EMPTY_KEY_SOURCES = (
    "unset-env",
    "empty-env",
    "blank-env",
    "missing-file",
    "empty-file",
    "comments-only-file",
)


@pytest.fixture(autouse=True)
def llama_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLAMA_API_KEYS", "hull_fixture_one")


@pytest.fixture(params=EMPTY_KEY_SOURCES)
def empty_key_source(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    keys_file = tmp_path / "route-keys"
    if request.param == "unset-env":
        monkeypatch.delenv("ROUTE_KEYS", raising=False)
        return "env:ROUTE_KEYS"
    if request.param == "empty-env":
        monkeypatch.setenv("ROUTE_KEYS", "")
        return "env:ROUTE_KEYS"
    if request.param == "blank-env":
        monkeypatch.setenv("ROUTE_KEYS", " , ,")
        return "env:ROUTE_KEYS"
    if request.param == "empty-file":
        keys_file.write_text("")
    if request.param == "comments-only-file":
        keys_file.write_text("# rotated keys go here\n\n  # one per line\n")
    return f"file:{keys_file}"


@pytest.fixture
def empty_key_raw(llama_raw: dict[str, Any], empty_key_source: str) -> dict[str, Any]:
    llama_raw["route"]["auth"]["apiKeys"]["from"] = empty_key_source
    return llama_raw


def no_keys_message(source: str) -> str:
    return f"route `llama-8b` sets auth.apiKeys but {source} resolved to no keys"


@pytest.fixture
def llama_spec() -> ServiceSpec:
    return specmod.load(FIXTURES / "llama-8b.yaml")


@pytest.fixture
def llama_raw() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "llama-8b.yaml").read_text())


@pytest.fixture
def mock_docker_spec() -> ServiceSpec:
    return specmod.load(FIXTURES / "mock-three-docker.yaml")


@pytest.fixture
def mock_docker_raw() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "mock-three-docker.yaml").read_text())


@pytest.fixture
def mock_kind_modal_spec() -> ServiceSpec:
    return specmod.load(FIXTURES / "mock-kind-modal.yaml")


@pytest.fixture
def target_for(llama_spec: ServiceSpec):
    def build(provider: str) -> Target:
        return Target(llama_spec, llama_spec.target(provider))

    return build


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeProvider]:
    fakes = {provider_type: FakeProvider() for provider_type in PROVIDER_TYPES}

    def factory_for(fake: FakeProvider) -> Callable[..., FakeProvider]:
        def factory(**kwargs: Any) -> FakeProvider:
            fake.kwargs = kwargs
            return fake

        return factory

    for provider_type, fake in fakes.items():
        monkeypatch.setitem(PROVIDERS, provider_type, factory_for(fake))
    return fakes


def assert_golden(name: str, actual: Any) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDENS") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".yaml":
            documents = actual if isinstance(actual, list) else [actual]
            path.write_text(yaml.safe_dump_all(documents, sort_keys=False))
        else:
            path.write_text(json.dumps(actual, indent=2, sort_keys=False) + "\n")
    if path.suffix == ".yaml":
        expected: Any = list(yaml.safe_load_all(path.read_text()))
        if not isinstance(actual, list):
            expected = expected[0]
    else:
        expected = json.loads(path.read_text())
    assert actual == expected
