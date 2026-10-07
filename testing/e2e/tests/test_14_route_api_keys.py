from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterator

import httpx
import pytest

from e2e.client import MODEL, NO_AUTH_KEY, ROUTE_HOST, RouterClient, failures, load, providers_of
from e2e.harness import Controller, Deployment, Router
from multihull.apikeys import hash_api_key

HASH = re.compile(r"[0-9a-f]{64}")


def fake_key() -> str:
    return f"hull_e2e{secrets.token_hex(4)}_{secrets.token_urlsafe(24)}"


def secret_of(key: str) -> str:
    return key.split("_", 2)[2]


@pytest.fixture(scope="module")
def route_keys() -> list[str]:
    return [fake_key(), fake_key()]


@pytest.fixture(scope="module", autouse=True)
def auth_controller(
    controller: Controller, deployment: Deployment, route_keys: list[str]
) -> Iterator[Controller]:
    deployment.route_keys_path.write_text("\n".join(route_keys) + "\n")
    controller.restart(deployment.auth_spec_path)
    try:
        yield controller
    finally:
        controller.restart(deployment.spec_path)
        deployment.route_keys_path.unlink(missing_ok=True)


def test_configured_keys_reach_the_upstream(
    router: Router, route_keys: list[str], stream: bool
) -> None:
    for key in route_keys:
        outcomes = load(RouterClient(router.base_url, api_key=key), 5, stream, concurrency=1)
        assert failures(outcomes) == []
        assert providers_of(outcomes) == {"primary": 5}


def test_wrong_and_missing_keys_get_401(router: Router, route_keys: list[str]) -> None:
    key_id = route_keys[0].split("_")[1]
    wrong = [
        f"hull_{key_id}_{secrets.token_urlsafe(24)}",
        f"{route_keys[0]}x",
        fake_key(),
        NO_AUTH_KEY,
    ]
    for index, key in enumerate(wrong):
        outcome = RouterClient(router.base_url, api_key=key).send(index)
        assert outcome.status == 401, f"wrong key {index}: {outcome.describe()}"
        assert outcome.provider is None
        assert json.loads(outcome.body)["error"]["type"] == "unauthorized"

    missing = httpx.post(
        f"{router.base_url}/v1/chat/completions",
        headers={"Host": ROUTE_HOST},
        json={"model": MODEL, "messages": [{"role": "user", "content": "no key"}]},
        timeout=10,
    )
    assert missing.status_code == 401
    assert "x-hull-provider" not in missing.headers
    assert router.metrics().requests(outcome="success") == 0


def test_snapshot_carries_only_hashes(
    auth_controller: Controller, router: Router, route_keys: list[str]
) -> None:
    load(RouterClient(router.base_url, api_key=route_keys[0]), 1, concurrency=1)
    document = auth_controller.snapshot()
    assert document is not None
    assert document["routes"][0]["auth"]["required"] is True
    hashes = document["routes"][0]["auth"]["api_key_hashes"]
    assert hashes == sorted(hash_api_key(key) for key in route_keys)
    assert all(HASH.fullmatch(digest) for digest in hashes)
    exposed = "\n".join(
        [
            auth_controller.snapshot_path.read_text(),
            auth_controller.log_text(),
            router.log_text(),
        ]
    )
    for key in route_keys:
        assert secret_of(key) not in exposed
