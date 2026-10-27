from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from live.baseten_sweep import main, stale_models
from multihull.providers.baseten import API_KEY_ENV, BasetenApi

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CALLER = "sweep-fixture-caller"


def model(name: str, created: str) -> dict[str, str]:
    return {"id": f"m-{name}", "name": name, "created_at": created}


def api_answering(handler: httpx.MockTransport) -> BasetenApi:
    return BasetenApi(httpx.Client(transport=handler))


def test_sweeper_picks_only_old_live_models() -> None:
    models = [
        model("multihull-live-1-baseten", "2026-10-07T09:00:00Z"),
        model("multihull-live-2-baseten", "2026-10-07T11:30:00Z"),
        model("multihull-prod-baseten", "2026-10-01T08:00:00Z"),
        model("multihull-live-4-baseten", "2026-10-07T11:00:00+02:00"),
    ]
    stale = stale_models(models, "multihull-live-", timedelta(hours=2), NOW)
    assert [m["name"] for m in stale] == ["multihull-live-1-baseten", "multihull-live-4-baseten"]
    current = stale_models(models, "multihull-live-2", timedelta(0), NOW)
    assert [m["name"] for m in current] == ["multihull-live-2-baseten"]


def test_sweeper_refuses_prefixes_outside_live_runs() -> None:
    with pytest.raises(SystemExit):
        main(["--prefix", "multihull-"])


def test_sweeper_deletes_matching_models(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    deleted: list[str] = []
    listed = [
        model("multihull-live-9-baseten", "2026-10-07T09:00:00Z"),
        model("multihull-live-10-baseten", "2026-10-07T09:00:00Z"),
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": listed})
        deleted.append(request.url.path)
        return httpx.Response(200, json={"id": "m", "deleted": True})

    assert main(["--prefix", "multihull-live-9-"], api_answering(httpx.MockTransport(answer))) == 0
    assert deleted == ["/v1/models/m-multihull-live-9-baseten"]
    output = capsys.readouterr().out.splitlines()
    assert output == [
        "m-multihull-live-9-baseten multihull-live-9-baseten deleted",
        "1 model(s) matched multihull-live-9-, 0 failed",
    ]


def test_sweeper_reports_a_rejected_key_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(API_KEY_ENV, CALLER)
    rejected = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": "Unauthorized"})
    )
    assert main(["--prefix", "multihull-live-1"], api_answering(rejected)) == 1
    assert capsys.readouterr().out == "skipped, Baseten API 401: Unauthorized\n"


def test_sweeper_without_a_key_skips(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    unused = httpx.MockTransport(lambda request: httpx.Response(500))
    assert main(["--prefix", "multihull-live-1"], api_answering(unused)) == 1
    assert capsys.readouterr().out.startswith("skipped, BASETEN_API_KEY is not set")
