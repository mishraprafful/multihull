from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from multihull import top
from multihull.cli import app
from tests.conftest import FIXTURES

TOP = FIXTURES / "top"
ROUTE = "e2e-three-top-capture"
PRIMARY = f"{ROUTE}/primary"
SECONDARY = f"{ROUTE}/secondary"
TERTIARY = f"{ROUTE}/tertiary"
ADMIN = "http://127.0.0.1:9090"


def payloads(name: str) -> tuple[str, dict[str, Any]]:
    metrics = (TOP / f"{name}.metrics.txt").read_text()
    endpoints = json.loads((TOP / f"{name}.endpoints.json").read_text())
    return metrics, endpoints


def snapshot(name: str, taken_at: float) -> top.Snapshot:
    metrics, endpoints = payloads(name)
    return top.build_snapshot(metrics, endpoints, taken_at)


def rendered(frame: top.Frame, width: int = 160) -> str:
    console = Console(record=True, width=width, force_terminal=False)
    console.print(top.render(frame))
    return console.export_text()


def column(text: str, table: str, header: str) -> list[str]:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(table))
    end = next(index for index in range(start, len(lines)) if lines[index].startswith("└"))
    section = lines[start:end]
    header_line = next(line for line in section if header in line)
    cells = [cell.strip() for cell in header_line.split("┃")]
    index = cells.index(header)
    return [line.split("│")[index].strip() for line in section if line.startswith("│")]


def test_parse_metrics_reads_labels_escapes_and_special_values() -> None:
    text = "\n".join(
        [
            "# HELP x help",
            "# TYPE x counter",
            'x{a="1",b="q\\"uote\\\\n\\n"} 3',
            "x 4",
            "y{} NaN",
            "z +Inf 1700000000",
            "garbage line without value",
            "",
        ]
    )
    samples = top.parse_metrics(text)
    assert [sample.name for sample in samples] == ["x", "x", "y", "z"]
    assert samples[0].labels == {"a": "1", "b": 'q"uote\\n\n'}
    assert samples[0].value == 3
    assert samples[1].labels == {}
    assert math.isnan(samples[2].value)
    assert samples[3].value == math.inf


def test_parse_metrics_on_a_router_capture() -> None:
    metrics = top.Metrics.parse(payloads("primary-down-1")[0])
    assert metrics.total(top.REQUESTS_TOTAL, endpoint=PRIMARY) == 150
    assert metrics.total(top.REQUESTS_TOTAL, endpoint=PRIMARY, outcome="transient") == 6
    assert metrics.value(top.FAILOVERS_TOTAL, reason="transient", **{"from": "primary"}) == 6
    assert metrics.value(top.UPSTREAM_TTFT_SECONDS, endpoint=TERTIARY, quantile="0.5") is None


def test_healthy_snapshot_shows_every_endpoint_serving() -> None:
    current = snapshot("healthy", 10.0)
    assert (
        current.version
        == json.loads((TOP / "healthy.endpoints.json").read_text())["snapshot_version"]
    )
    assert [endpoint.id for endpoint in current.endpoints] == [PRIMARY, SECONDARY, TERTIARY]
    assert {endpoint.state for endpoint in current.endpoints} == {"serving"}
    primary = current.endpoint(PRIMARY)
    assert primary is not None
    assert primary.provider == "primary"
    assert primary.circuit == "closed"
    assert primary.requests == 60
    assert primary.outstanding == 0
    assert primary.ttft_p50 == pytest.approx(0.0927, abs=0.001)
    assert primary.ttft_p95 == pytest.approx(0.0955, abs=0.001)
    tertiary = current.endpoint(TERTIARY)
    assert tertiary is not None
    assert tertiary.requests == 0
    assert tertiary.ttft_p50 is None and tertiary.ttft_p95 is None
    route = current.route(ROUTE)
    assert route is not None
    assert route.responses == 60
    assert route.errors == 0
    assert route.failovers == {}


def test_stopped_primary_reads_down_and_failovers_land_on_its_route() -> None:
    current = snapshot("primary-down-1", 10.0)
    primary = current.endpoint(PRIMARY)
    secondary = current.endpoint(SECONDARY)
    assert primary is not None and secondary is not None
    assert primary.health == "down"
    assert primary.circuit == "open"
    assert primary.state == "down"
    assert secondary.state == "serving"
    assert secondary.outstanding == 4
    assert secondary.requests == 92
    route = current.route(ROUTE)
    assert route is not None
    assert route.responses == 236
    assert route.errors == 0
    assert route.failovers == {"transient": 6}


@pytest.mark.parametrize(
    ("health", "circuit", "probe", "state"),
    [
        ("ready", "closed", "up", "serving"),
        ("ready", None, None, "serving"),
        ("degraded", "closed", "unknown", "serving"),
        ("ready", "half-open", "up", "recovering"),
        ("ready", "open", "up", "down"),
        ("ready", "closed", "down", "down"),
        ("down", "closed", "up", "down"),
        ("down", "half-open", "up", "down"),
    ],
)
def test_state_vocabulary(health: str, circuit: str | None, probe: str | None, state: str) -> None:
    assert top.state_of(health, circuit, probe) == state


def test_rates_come_from_counter_deltas_between_polls() -> None:
    previous = snapshot("primary-down-1", 100.0)
    current = snapshot("primary-down-2", 102.0)
    frame = top.Frame(ADMIN, current, previous)
    assert frame.interval == 2.0
    primary = current.endpoint(PRIMARY)
    secondary = current.endpoint(SECONDARY)
    assert primary is not None and secondary is not None
    assert frame.endpoint_rate(primary) == 0.0
    assert frame.endpoint_rate(secondary) == 42.0
    route = current.route(ROUTE)
    assert route is not None
    assert frame.route_rate(route) == (42.0, 0.0)
    assert frame.failover_delta(route, "transient") == 0.0


def test_counter_reset_and_missing_history_give_no_rate() -> None:
    assert top.rate(10.0, None, 1.0) is None
    assert top.rate(5.0, 10.0, 1.0) is None
    assert top.rate(10.0, 10.0, 0.0) is None
    assert top.rate(12.0, 10.0, 4.0) == 0.5


def test_first_frame_renders_dashes_not_zeros() -> None:
    text = rendered(top.Frame(ADMIN, snapshot("healthy", 1.0), None))
    assert column(text, "Endpoints", "req/s") == ["-", "-", "-"]
    assert column(text, "Routes", "req/s") == ["-"]
    assert column(text, "Routes", "errors/s") == ["-"]
    assert "connected" in text
    assert "snapshot v" in text


def test_second_frame_renders_rates_states_and_failovers() -> None:
    frame = top.Frame(ADMIN, snapshot("primary-down-2", 4.0), snapshot("primary-down-1", 2.0))
    text = rendered(frame)
    assert column(text, "Endpoints", "state") == ["down", "serving", "serving"]
    assert column(text, "Endpoints", "circuit") == ["open", "closed", "closed"]
    assert column(text, "Endpoints", "req/s") == ["0.0", "42.0", "0.0"]
    assert column(text, "Routes", "req/s") == ["42.0"]
    assert column(text, "Endpoints", "in-flight") == ["0", "4", "0"]
    assert column(text, "Endpoints", "ttft p50") == ["94 ms", "94 ms", "-"]
    assert column(text, "Routes", "failovers") == ["transient 6"]


def test_unreachable_router_keeps_the_last_frame_and_says_so() -> None:
    frame = top.Frame(ADMIN, snapshot("healthy", 1.0), None, error="ConnectError")
    text = rendered(frame)
    assert "router unreachable, retrying" in text
    assert "ConnectError" in text
    assert PRIMARY in text
    empty = rendered(top.Frame(ADMIN, None, None, error="ConnectError"))
    assert "router unreachable, retrying" in empty
    assert "waiting for the first poll" in empty


def test_run_top_retries_after_a_disconnect_and_stops_after_the_frames() -> None:
    calls: list[int] = []
    slept: list[float] = []

    def fetch() -> tuple[str, dict[str, Any]]:
        calls.append(len(calls))
        if len(calls) == 2:
            raise httpx.ConnectError("refused")
        return payloads("healthy") if len(calls) == 1 else payloads("primary-down-1")

    console = Console(record=True, width=160, force_terminal=False)
    top.run_top(
        ADMIN,
        0.5,
        console,
        fetch=fetch,
        frames=3,
        clock=iter([0.0, 1.0, 2.0]).__next__,
        sleep=slept.append,
    )
    text = console.export_text()
    assert calls == [0, 1, 2]
    assert slept == [0.5, 0.5]
    assert "connected" in text
    assert column(text, "Endpoints", "state") == ["down", "serving", "serving"]


def test_run_top_never_crashes_while_the_router_stays_down() -> None:
    def fetch() -> tuple[str, dict[str, Any]]:
        raise httpx.ConnectError("refused")

    console = Console(record=True, width=160, force_terminal=False)
    top.run_top(ADMIN, 0.1, console, fetch=fetch, frames=3, sleep=lambda _: None)
    assert "router unreachable, retrying" in console.export_text()


def test_run_top_exits_cleanly_on_ctrl_c() -> None:
    def fetch() -> tuple[str, dict[str, Any]]:
        raise KeyboardInterrupt

    console = Console(record=True, width=160, force_terminal=False)
    top.run_top(ADMIN, 0.1, console, fetch=fetch, sleep=lambda _: None)


def test_admin_client_fetches_both_admin_endpoints() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/metrics":
            return httpx.Response(200, text=payloads("healthy")[0])
        return httpx.Response(200, json=payloads("healthy")[1])

    client = top.AdminClient("http://router.test:9090/")
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    with client:
        metrics, endpoints = client.fetch()
    assert seen == ["/metrics", "/debug/endpoints"]
    assert top.REQUESTS_TOTAL in metrics
    assert len(endpoints["endpoints"]) == 3


def test_cli_top_passes_admin_and_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[str, float]] = []
    monkeypatch.setattr(
        top, "run_top", lambda admin, interval, console: received.append((admin, interval))
    )
    result = CliRunner().invoke(app, ["top", "--admin", "http://router:9090", "--interval", "0.5"])
    assert result.exit_code == 0, result.output
    assert received == [("http://router:9090", 0.5)]
    default = CliRunner().invoke(app, ["top"])
    assert default.exit_code == 0, default.output
    assert received[-1] == (top.DEFAULT_ADMIN, top.DEFAULT_INTERVAL)
    assert CliRunner().invoke(app, ["top", "--interval", "0"]).exit_code != 0
