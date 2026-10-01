from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from multihull import spec as specmod
from multihull.providers.base import Target
from multihull.spec import ServiceSpec

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def llama_spec() -> ServiceSpec:
    return specmod.load(FIXTURES / "llama-8b.yaml")


@pytest.fixture
def llama_raw() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "llama-8b.yaml").read_text())


@pytest.fixture
def target_for(llama_spec: ServiceSpec):
    def build(provider: str) -> Target:
        return Target(llama_spec, llama_spec.target(provider))

    return build


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
