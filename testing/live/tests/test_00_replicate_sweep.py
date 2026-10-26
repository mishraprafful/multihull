from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from live.replicate_sweep import main, stale_deployments
from multihull.providers.replicate import API_TOKEN_ENV, ReplicateApi

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
CALLER = "sweep-fixture-caller"


def deployment(name: str, released: str, warm: int = 0) -> dict[str, Any]:
    return {
        "owner": "acme",
        "name": name,
        "current_release": {
            "created_at": released,
            "configuration": {"min_instances": warm, "max_instances": warm, "hardware": "cpu"},
        },
    }


def api_answering(handler: httpx.MockTransport) -> ReplicateApi:
    return ReplicateApi(httpx.Client(transport=handler))


def test_sweeper_picks_only_old_live_deployments() -> None:
    deployments = [
        deployment("multihull-live-replicate-replicate", "2026-10-26T09:00:00Z"),
        deployment("multihull-live-other-replicate", "2026-10-26T11:30:00Z"),
        deployment("multihull-prod-replicate", "2026-10-01T08:00:00Z"),
        deployment("multihull-live-tz-replicate", "2026-10-26T11:00:00+02:00"),
    ]
    stale = stale_deployments(deployments, "multihull-live-", timedelta(hours=2), NOW)
    assert [d["name"] for d in stale] == [
        "multihull-live-replicate-replicate",
        "multihull-live-tz-replicate",
    ]
    current = stale_deployments(deployments, "multihull-live-other", timedelta(0), NOW)
    assert [d["name"] for d in current] == ["multihull-live-other-replicate"]


def test_sweeper_refuses_prefixes_outside_live_runs() -> None:
    with pytest.raises(SystemExit):
        main(["--prefix", "multihull-"])


def test_sweeper_scales_warm_deployments_down_and_deletes_offline_ones(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, CALLER)
    listed = [
        deployment("multihull-live-warm-replicate", "2026-10-26T09:00:00Z", warm=1),
        deployment("multihull-live-idle-replicate", "2026-10-26T09:00:00Z"),
        deployment("multihull-live-busy-replicate", "2026-10-26T09:00:00Z"),
    ]
    writes: list[tuple[str, str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"next": None, "previous": None, "results": listed})
        body = json.loads(request.content) if request.content else None
        writes.append((request.method, request.url.path, body))
        if request.url.path.endswith("busy-replicate"):
            return httpx.Response(409, json={"title": "Conflict", "detail": "used recently"})
        return httpx.Response(204 if request.method == "DELETE" else 200, json={})

    api = api_answering(httpx.MockTransport(answer))
    assert main(["--prefix", "multihull-live-"], api) == 0
    assert writes == [
        (
            "PATCH",
            "/v1/deployments/acme/multihull-live-warm-replicate",
            {"min_instances": 0, "max_instances": 0},
        ),
        ("DELETE", "/v1/deployments/acme/multihull-live-idle-replicate", None),
        ("DELETE", "/v1/deployments/acme/multihull-live-busy-replicate", None),
    ]
    assert capsys.readouterr().out.splitlines() == [
        "acme/multihull-live-warm-replicate scaled to zero; a later sweep deletes it",
        "acme/multihull-live-idle-replicate deleted",
        "acme/multihull-live-busy-replicate delete deferred: "
        "Replicate API 409: Conflict: used recently",
        "3 deployment(s) matched multihull-live-, 0 failed",
    ]


def test_sweeper_without_a_token_skips(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    unused = httpx.MockTransport(lambda request: httpx.Response(500))
    assert main(["--prefix", "multihull-live-"], api_answering(unused)) == 1
    assert capsys.readouterr().out.startswith(f"skipped, {API_TOKEN_ENV} is not set")
