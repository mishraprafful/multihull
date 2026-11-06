from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest

from tests.openapi_contract import SPECS, VIOLATIONS, SpecViolation, contract

RUNPOD = "https://api.runpod.io/v2"
REPLICATE = "https://api.replicate.com/v1"
EMPTY_LISTING = {"endpoints": [], "pagination": {"nextCursor": None, "hasNextPage": False}}
EMPTY_PAGE = {"next": None, "previous": None, "results": []}


def answer(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status) if body is None else httpx.Response(status, json=body)


def refused(provider: str, request: httpx.Request, response: httpx.Response) -> str:
    with pytest.raises(SpecViolation) as caught:
        contract(provider).exchange(request, lambda _: response)
    VIOLATIONS.clear()
    return str(caught.value)


def endpoint_body(**extra: Any) -> dict[str, Any]:
    return {
        "name": "multihull-a-runpod",
        "type": "LOAD_BALANCER",
        "image": "ghcr.io/acme/server:1",
        "scaling": {"type": "REQUEST_COUNT", "requestCount": 1},
        **extra,
    }


def test_an_undocumented_request_field_names_the_operation_and_field_but_no_value() -> None:
    value = secrets.token_hex(8)
    request = httpx.Request("POST", f"{RUNPOD}/serverless", json=endpoint_body(bogus=value))
    message = refused("runpod", request, answer(204))
    assert message.startswith("Runpod REST API POST /v2/serverless (createEndpoint): ")
    assert "request body bogus: undocumented field" in message
    assert value not in message


def test_a_secret_name_outside_the_pattern_never_echoes_the_name_or_value() -> None:
    name, value = f"1{secrets.token_hex(6)}", secrets.token_hex(16)
    body = {"name": name, "value": value}
    message = refused(
        "runpod", httpx.Request("POST", f"{RUNPOD}/account/secrets", json=body), answer(204)
    )
    assert "request body name: breaks pattern" in message
    assert name not in message
    assert value not in message


def test_a_value_outside_an_enum_or_limit_is_refused() -> None:
    body = endpoint_body(type="LB", workers={"min": 0, "max": 1, "idleTimeout": 0})
    message = refused(
        "runpod", httpx.Request("POST", f"{RUNPOD}/serverless", json=body), answer(204)
    )
    assert "request body type: breaks enum" in message
    assert "request body workers/idleTimeout: breaks minimum 1" in message


def test_query_parameters_must_be_documented_and_valid() -> None:
    request = httpx.Request("GET", f"{RUNPOD}/serverless", params={"limit": "5000", "page": "2"})
    message = refused("runpod", request, answer(200, EMPTY_LISTING))
    assert "query parameter page: undocumented" in message
    assert "query parameter limit (root): breaks maximum 1000" in message


def test_a_path_outside_the_spec_is_refused_without_its_ids() -> None:
    endpoint = secrets.token_hex(6)
    request = httpx.Request("GET", f"{RUNPOD}/serverless/{endpoint}/jobs")
    message = refused("runpod", request, answer(200, {}))
    assert "GET /v2/serverless/{?}/{?}: no operation in the spec matches" in message
    assert endpoint not in message


def test_a_fake_response_missing_a_required_field_is_refused() -> None:
    request = httpx.Request("GET", f"{RUNPOD}/serverless", params={"limit": "1"})
    message = refused("runpod", request, answer(200, {"endpoints": []}))
    assert "response 200 body (root): missing required field pagination" in message


def test_a_fake_response_with_an_undocumented_field_is_refused() -> None:
    request = httpx.Request("GET", f"{RUNPOD}/serverless", params={"limit": "1"})
    message = refused("runpod", request, answer(200, {**EMPTY_LISTING, "total": 0}))
    assert "response 200 body total: undocumented field" in message


def test_an_undocumented_success_status_is_refused() -> None:
    request = httpx.Request("GET", f"{RUNPOD}/serverless", params={"limit": "1"})
    message = refused("runpod", request, answer(201, EMPTY_LISTING))
    assert "response 201 (root): status not documented" in message


def test_the_cursor_allowance_covers_only_the_deployment_listing() -> None:
    listing = httpx.Request("GET", f"{REPLICATE}/deployments", params={"cursor": "c1"})
    assert contract("replicate").exchange(listing, lambda _: answer(200, EMPTY_PAGE))
    model = httpx.Request("GET", f"{REPLICATE}/models/acme/echo", params={"cursor": "c1"})
    message = refused("replicate", model, answer(200, {"owner": "acme", "name": "echo"}))
    assert "query parameter cursor: undocumented" in message


def test_traffic_outside_the_management_host_passes_unchecked() -> None:
    request = httpx.Request("GET", "https://ep1.api.runpod.ai/anything")
    assert contract("runpod").exchange(request, lambda _: answer(418)).status_code == 418


def test_a_conforming_exchange_passes() -> None:
    request = httpx.Request("GET", f"{RUNPOD}/serverless", params={"limit": "1000", "cursor": "x"})
    response = contract("runpod").exchange(request, lambda _: answer(200, EMPTY_LISTING))
    assert response.status_code == 200


@pytest.mark.parametrize("provider", ["baseten", "replicate", "runpod"])
def test_every_vendored_spec_loads_and_has_a_dated_source(provider: str) -> None:
    loaded = contract(provider)
    readme = (SPECS / "README.md").read_text()
    assert loaded.operations
    assert any(line.startswith(f"| {provider} | https://") for line in readme.splitlines())
