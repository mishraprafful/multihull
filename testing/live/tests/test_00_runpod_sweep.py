from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from live.runpod_sweep import main, stale_endpoints, stale_stores
from multihull.providers.runpod import API_KEY_ENV, RunpodApi

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CALLER = "sweep-fixture-caller"


def endpoint(name: str, created: str) -> dict[str, str]:
    return {"id": f"ep-{name}", "name": name, "createdAt": created}


def api_answering(handler: httpx.MockTransport) -> RunpodApi:
    return RunpodApi(httpx.Client(transport=handler))


def test_sweeper_picks_only_old_live_endpoints() -> None:
    endpoints = [
        endpoint("multihull-live-1-runpod", "2026-10-07T09:00:00Z"),
        endpoint("multihull-live-2-runpod", "2026-10-07T11:30:00Z"),
        endpoint("multihull-prod-runpod", "2026-10-01T08:00:00Z"),
        endpoint("multihull-live-4-runpod", "2026-10-07T11:00:00+02:00"),
    ]
    stale = stale_endpoints(endpoints, "multihull-live-", timedelta(hours=2), NOW)
    assert [e["name"] for e in stale] == ["multihull-live-1-runpod", "multihull-live-4-runpod"]
    current = stale_endpoints(endpoints, "multihull-live-2", timedelta(0), NOW)
    assert [e["name"] for e in current] == ["multihull-live-2-runpod"]


def test_sweeper_refuses_prefixes_outside_live_runs() -> None:
    with pytest.raises(SystemExit):
        main(["--prefix", "multihull-"])


def test_sweeper_deletes_matching_endpoints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    deleted: list[str] = []
    listed = [
        endpoint("multihull-live-9-runpod", "2026-10-07T09:00:00Z"),
        endpoint("multihull-live-10-runpod", "2026-10-07T09:00:00Z"),
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            pagination = {"nextCursor": None, "hasNextPage": False}
            return httpx.Response(200, json={"endpoints": listed, "pagination": pagination})
        deleted.append(request.url.path)
        return httpx.Response(204)

    assert main(["--prefix", "multihull-live-9-"], api_answering(httpx.MockTransport(answer))) == 0
    assert deleted == ["/v2/serverless/ep-multihull-live-9-runpod"]
    output = capsys.readouterr().out.splitlines()
    assert output == [
        "ep-multihull-live-9-runpod multihull-live-9-runpod deleted",
        "1 endpoint(s) matched multihull-live-9-, 0 failed",
        "0 mirrored secret(s) matched multihull_live_9_, 0 failed",
    ]


def test_sweeper_reports_a_rejected_key_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    problem = {"title": "Unauthorized", "status": 401, "detail": "invalid bearer token"}
    rejected = httpx.MockTransport(lambda request: httpx.Response(401, json=problem))
    assert main(["--prefix", "multihull-live-1"], api_answering(rejected)) == 1
    assert capsys.readouterr().out == (
        "skipped, RunPod API 401: Unauthorized: invalid bearer token\n"
    )


def test_sweeper_without_a_key_skips(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    unused = httpx.MockTransport(lambda request: httpx.Response(500))
    assert main(["--prefix", "multihull-live-1"], api_answering(unused)) == 1
    assert capsys.readouterr().out.startswith("skipped, RUNPOD_API_KEY is not set")


def stored(name: str, created: str | None = None) -> dict[str, str]:
    found = {"id": f"sec-{name}", "name": name}
    if created:
        found["createdAt"] = created
    return found


def test_sweeper_picks_mirrored_secrets_by_their_sanitized_prefix() -> None:
    listed = [
        stored("multihull_live_c7_ab12_runpod_conformance_secret"),
        stored("team_shared"),
        stored("multihull_live_1_runpod_hf_token", "2026-10-07T09:00:00Z"),
        stored("multihull_live_2_runpod_hf_token", "2026-10-07T11:30:00Z"),
    ]
    current = stale_stores(listed, "multihull-live-c7-", timedelta(0), NOW)
    assert [s["name"] for s in current] == ["multihull_live_c7_ab12_runpod_conformance_secret"]
    stale = stale_stores(listed, "multihull-live-", timedelta(hours=2), NOW)
    assert [s["name"] for s in stale] == ["multihull_live_1_runpod_hf_token"]


def test_sweeper_deletes_mirrored_secrets_left_without_an_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    deleted: list[str] = []
    listed = [stored("multihull_live_9_runpod_hf_token"), stored("team_shared")]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v2/account/secrets":
            return httpx.Response(200, json={"secrets": listed})
        if request.method == "GET":
            pagination = {"nextCursor": None, "hasNextPage": False}
            return httpx.Response(200, json={"endpoints": [], "pagination": pagination})
        deleted.append(request.url.path)
        return httpx.Response(204)

    assert main(["--prefix", "multihull-live-9-"], api_answering(httpx.MockTransport(answer))) == 0
    assert deleted == ["/v2/account/secrets/sec-multihull_live_9_runpod_hf_token"]
    summary = capsys.readouterr().out.splitlines()[-1]
    assert summary == "1 mirrored secret(s) matched multihull_live_9_, 0 failed"
