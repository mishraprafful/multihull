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
