from __future__ import annotations

import re
from secrets import token_hex
from typing import Any

import pytest

from multihull.providers.base import Target, secret_env_name, store_name
from multihull.providers.runpod import API_KEY_ENV, RunpodProvider, secret_store_name
from multihull.spec import ServiceSpec
from tests.fake_runpod import FakeRunpod

RUNPOD_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.\-/]*$")
RUNPOD_NAME_LIMIT = 191
ALIKE = [
    (("a", "b-c", "d"), ("a-b", "c", "d")),
    (("llama", "baseten", "hf-token"), ("llama-baseten", "us", "hf-token")),
    (("svc", "runpod", "hf-token"), ("svc", "runpod", "hf_token")),
]


def normalised(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name).lower()


@pytest.mark.parametrize(("one", "other"), ALIKE)
def test_names_that_sanitise_alike_stay_distinct(
    one: tuple[str, str, str], other: tuple[str, str, str]
) -> None:
    assert normalised(store_name(*one)) != normalised(store_name(*other))


def test_names_keep_a_readable_prefix_within_provider_limits() -> None:
    assert store_name("llama-8b", "runpod-eu", "hf-token").startswith(
        "multihull_llama_8b_runpod_eu_hf_token_"
    )
    longest = store_name("s" * 40, "p" * 300, "x" * 300)
    assert len(longest) <= RUNPOD_NAME_LIMIT
    assert RUNPOD_NAME.match(longest) and re.fullmatch(r"[A-Za-z0-9_]+", longest)
    assert not longest.upper().startswith("RUNPOD")


def test_runpod_targets_that_sanitise_alike_keep_separate_secrets(
    monkeypatch: pytest.MonkeyPatch, llama_raw: dict[str, Any]
) -> None:
    caller = token_hex(8)
    monkeypatch.setenv(API_KEY_ENV, caller)
    for name in llama_raw["container"]["secrets"]:
        monkeypatch.setenv(secret_env_name(name), token_hex(8))
    fake = FakeRunpod(caller)
    deployed: list[tuple[RunpodProvider, Target]] = []
    for service, provider_name in (("a", "b-c"), ("a-b", "c")):
        target = {"provider": provider_name, "type": "runpod", "priority": 9}
        spec = ServiceSpec.model_validate(
            {**llama_raw, "name": service, "targets": [*llama_raw["targets"], target]}
        )
        provider = RunpodProvider(False, provider_name, fake.client())
        deployed.append((provider, Target(spec, spec.target(provider_name))))
    refs = [provider.apply(desired, None) for provider, desired in deployed]
    assert len(fake.secrets) == 2 * len(llama_raw["container"]["secrets"])
    deployed[0][0].destroy(refs[0])
    survivor = deployed[1][1]
    kept = sorted(secret_store_name(survivor, name) for name in survivor.service.container.secrets)
    assert sorted(stored["name"] for stored in fake.secrets.values()) == kept
