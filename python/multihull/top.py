from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

DEFAULT_ADMIN = "http://127.0.0.1:9090"
DEFAULT_INTERVAL = 1.0
DASH = "-"

REQUESTS_TOTAL = "router_requests_total"
RESPONSES_TOTAL = "router_responses_total"
FAILOVERS_TOTAL = "router_failovers_total"
UPSTREAM_TTFT_SECONDS = "router_upstream_ttft_seconds"

SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
ESCAPE = re.compile(r"\\(.)")
ESCAPES = {"n": "\n", '"': '"', "\\": "\\"}

STATE_STYLES = {
    "serving": "green",
    "recovering": "yellow",
    "down": "red",
    "unknown": "dim",
}

Fetcher = Callable[[], tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class Sample:
    name: str
    labels: Mapping[str, str]
    value: float


def unescape(value: str) -> str:
    return ESCAPE.sub(lambda match: ESCAPES.get(match.group(1), match.group(0)), value)


def parse_value(raw: str) -> float:
    lowered = raw.lower()
    if lowered in ("nan", "+nan", "-nan"):
        return float("nan")
    if lowered in ("+inf", "inf"):
        return float("inf")
    if lowered == "-inf":
        return float("-inf")
    return float(raw)


def parse_metrics(text: str) -> list[Sample]:
    samples: list[Sample] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = SAMPLE.match(stripped)
        if match is None:
            continue
        name, raw_labels, raw_value = match.groups()
        labels = {key: unescape(value) for key, value in LABEL.findall(raw_labels or "")}
        try:
            value = parse_value(raw_value)
        except ValueError:
            continue
        samples.append(Sample(name, labels, value))
    return samples


@dataclass(frozen=True)
class Metrics:
    samples: tuple[Sample, ...]

    @classmethod
    def parse(cls, text: str) -> Metrics:
        return cls(tuple(parse_metrics(text)))

    def series(self, name: str, **labels: str) -> list[Sample]:
        return [
            sample
            for sample in self.samples
            if sample.name == name
            and all(sample.labels.get(key) == value for key, value in labels.items())
        ]

    def total(self, name: str, **labels: str) -> float:
        return sum(sample.value for sample in self.series(name, **labels))

    def value(self, name: str, **labels: str) -> float | None:
        matched = self.series(name, **labels)
        return matched[0].value if matched else None


def state_of(health: str, circuit: str | None, probe: str | None) -> str:
    if circuit == "open" or probe == "down" or health == "down":
        return "down"
    if circuit == "half-open":
        return "recovering"
    if circuit in (None, "closed") and health in ("ready", "degraded", "draining", "unspecified"):
        return "serving"
    return "unknown"


@dataclass(frozen=True)
class EndpointStats:
    route: str
    id: str
    provider: str
    health: str
    circuit: str | None
    probe: str | None
    outstanding: int | None
    requests: float
    ttft_p50: float | None
    ttft_p95: float | None

    @property
    def state(self) -> str:
        return state_of(self.health, self.circuit, self.probe)


@dataclass(frozen=True)
class RouteStats:
    id: str
    responses: float
    errors: float
    failovers: Mapping[str, float]


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    version: int | None
    endpoints: tuple[EndpointStats, ...]
    routes: tuple[RouteStats, ...]

    def endpoint(self, endpoint_id: str) -> EndpointStats | None:
        return next((entry for entry in self.endpoints if entry.id == endpoint_id), None)

    def route(self, route_id: str) -> RouteStats | None:
        return next((entry for entry in self.routes if entry.id == route_id), None)


def ttft_quantile(metrics: Metrics, endpoint_id: str, quantile: str) -> float | None:
    if metrics.total(f"{UPSTREAM_TTFT_SECONDS}_count", endpoint=endpoint_id) <= 0:
        return None
    value = metrics.value(UPSTREAM_TTFT_SECONDS, endpoint=endpoint_id, quantile=quantile)
    if value is None or value != value:
        return None
    return value


def endpoint_stats(entry: Mapping[str, Any], metrics: Metrics) -> EndpointStats:
    endpoint_id = str(entry["id"])
    probe = entry.get("probe") or {}
    outstanding = entry.get("outstanding")
    return EndpointStats(
        route=str(entry.get("route", "")),
        id=endpoint_id,
        provider=str(entry.get("provider", "")),
        health=str(entry.get("health", "unspecified")),
        circuit=entry.get("circuit"),
        probe=probe.get("state"),
        outstanding=int(outstanding) if outstanding is not None else None,
        requests=metrics.total(REQUESTS_TOTAL, endpoint=endpoint_id),
        ttft_p50=ttft_quantile(metrics, endpoint_id, "0.5"),
        ttft_p95=ttft_quantile(metrics, endpoint_id, "0.95"),
    )


def route_stats(route_id: str, providers: set[str], metrics: Metrics) -> RouteStats:
    responses = 0.0
    errors = 0.0
    for sample in metrics.series(RESPONSES_TOTAL, route=route_id):
        responses += sample.value
        if sample.labels.get("status", "0").isdigit() and int(sample.labels["status"]) >= 400:
            errors += sample.value
    failovers: dict[str, float] = {}
    for sample in metrics.series(FAILOVERS_TOTAL):
        if sample.labels.get("from") not in providers:
            continue
        reason = sample.labels.get("reason", "unknown")
        failovers[reason] = failovers.get(reason, 0.0) + sample.value
    return RouteStats(route_id, responses, errors, dict(sorted(failovers.items())))


def build_snapshot(
    metrics_text: str, endpoints_payload: Mapping[str, Any], taken_at: float
) -> Snapshot:
    metrics = Metrics.parse(metrics_text)
    endpoints = tuple(
        endpoint_stats(entry, metrics) for entry in endpoints_payload.get("endpoints", [])
    )
    providers_by_route: dict[str, set[str]] = {}
    claimed: set[str] = set()
    for endpoint in endpoints:
        providers = providers_by_route.setdefault(endpoint.route, set())
        if endpoint.provider not in claimed:
            providers.add(endpoint.provider)
            claimed.add(endpoint.provider)
    routes = tuple(
        route_stats(route_id, providers, metrics)
        for route_id, providers in providers_by_route.items()
    )
    version = endpoints_payload.get("snapshot_version")
    return Snapshot(taken_at, int(version) if version is not None else None, endpoints, routes)


def rate(current: float, previous: float | None, seconds: float) -> float | None:
    if previous is None or seconds <= 0:
        return None
    delta = current - previous
    if delta < 0:
        return None
    return delta / seconds


@dataclass(frozen=True)
class Frame:
    admin: str
    current: Snapshot | None
    previous: Snapshot | None
    error: str | None = None

    @property
    def interval(self) -> float:
        if self.current is None or self.previous is None:
            return 0.0
        return self.current.taken_at - self.previous.taken_at

    def endpoint_rate(self, endpoint: EndpointStats) -> float | None:
        before = self.previous.endpoint(endpoint.id) if self.previous else None
        return rate(endpoint.requests, before.requests if before else None, self.interval)

    def route_rate(self, route: RouteStats) -> tuple[float | None, float | None]:
        before = self.previous.route(route.id) if self.previous else None
        return (
            rate(route.responses, before.responses if before else None, self.interval),
            rate(route.errors, before.errors if before else None, self.interval),
        )

    def failover_delta(self, route: RouteStats, reason: str) -> float | None:
        before = self.previous.route(route.id) if self.previous else None
        if before is None:
            return None
        delta = route.failovers.get(reason, 0.0) - before.failovers.get(reason, 0.0)
        return delta if delta >= 0 else None


def format_rate(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{value:.1f}"


def format_seconds(value: float | None) -> str:
    if value is None:
        return DASH
    if value < 1:
        return f"{value * 1000:.0f} ms"
    return f"{value:.2f} s"


def format_count(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{value:.0f}"


def format_failovers(frame: Frame, route: RouteStats) -> Text:
    if not route.failovers:
        return Text(DASH, style="dim")
    text = Text()
    for index, (reason, count) in enumerate(route.failovers.items()):
        if index:
            text.append("  ")
        text.append(f"{reason} ", style="dim")
        text.append(format_count(count))
        delta = frame.failover_delta(route, reason)
        if delta:
            text.append(f" +{delta:.0f}", style="yellow")
    return text


def state_text(state: str) -> Text:
    return Text(state, style=STATE_STYLES.get(state, ""))


def endpoints_table(frame: Frame, snapshot: Snapshot) -> Table:
    table = Table(title="Endpoints", title_justify="left", pad_edge=False)
    table.add_column("endpoint", overflow="fold")
    table.add_column("provider")
    table.add_column("state")
    table.add_column("circuit")
    table.add_column("probe")
    table.add_column("req/s", justify="right")
    table.add_column("in-flight", justify="right")
    table.add_column("ttft p50", justify="right")
    table.add_column("ttft p95", justify="right")
    for endpoint in snapshot.endpoints:
        table.add_row(
            endpoint.id,
            endpoint.provider,
            state_text(endpoint.state),
            endpoint.circuit or DASH,
            endpoint.probe or DASH,
            format_rate(frame.endpoint_rate(endpoint)),
            format_count(endpoint.outstanding),
            format_seconds(endpoint.ttft_p50),
            format_seconds(endpoint.ttft_p95),
        )
    if not snapshot.endpoints:
        table.add_row(Text("no endpoints in the snapshot", style="dim"), *[""] * 8)
    return table


def routes_table(frame: Frame, snapshot: Snapshot) -> Table:
    table = Table(title="Routes", title_justify="left", pad_edge=False)
    table.add_column("route")
    table.add_column("req/s", justify="right")
    table.add_column("errors/s", justify="right")
    table.add_column("errors", justify="right")
    table.add_column("failovers")
    for route in snapshot.routes:
        requests, errors = frame.route_rate(route)
        table.add_row(
            route.id,
            format_rate(requests),
            format_rate(errors),
            format_count(route.errors),
            format_failovers(frame, route),
        )
    return table


def header(frame: Frame) -> Text:
    text = Text()
    text.append("hull top", style="bold")
    text.append(f"  {frame.admin}", style="dim")
    if frame.current is not None and frame.current.version is not None:
        text.append(f"  snapshot v{frame.current.version}", style="dim")
    if frame.error is None:
        text.append("  connected", style="green")
    else:
        text.append("  router unreachable, retrying", style="bold red")
        text.append(f"  {frame.error}", style="dim")
    return text


def render(frame: Frame) -> RenderableType:
    parts: list[RenderableType] = [header(frame)]
    if frame.current is None:
        parts.append(Text("waiting for the first poll", style="dim"))
        return Group(*parts)
    parts.append(endpoints_table(frame, frame.current))
    parts.append(routes_table(frame, frame.current))
    return Group(*parts)


class AdminClient:
    def __init__(self, admin: str, timeout: float = 2.0) -> None:
        self.admin = admin.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def fetch(self) -> tuple[str, dict[str, Any]]:
        metrics = self.http.get(f"{self.admin}/metrics")
        metrics.raise_for_status()
        endpoints = self.http.get(f"{self.admin}/debug/endpoints")
        endpoints.raise_for_status()
        return metrics.text, endpoints.json()

    def __enter__(self) -> AdminClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.http.close()


def describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.request.url.path} returned {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return exc.__class__.__name__
    return str(exc) or exc.__class__.__name__


def next_frame(admin: str, fetch: Fetcher, previous: Snapshot | None, now: float) -> Frame:
    try:
        metrics_text, endpoints_payload = fetch()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return Frame(admin, previous, None, error=describe_error(exc))
    current = build_snapshot(metrics_text, endpoints_payload, now)
    return Frame(admin, current, previous)


def run_top(
    admin: str = DEFAULT_ADMIN,
    interval: float = DEFAULT_INTERVAL,
    console: Console | None = None,
    fetch: Fetcher | None = None,
    frames: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if fetch is None:
        with AdminClient(admin) as client:
            run_top(admin, interval, console, client.fetch, frames, clock, sleep)
        return
    console = console or Console()
    previous: Snapshot | None = None
    polled = 0
    try:
        with Live(render(Frame(admin, None, None)), console=console, refresh_per_second=4) as live:
            while frames is None or polled < frames:
                frame = next_frame(admin, fetch, previous, clock())
                live.update(render(frame))
                if frame.error is None:
                    previous = frame.current
                polled += 1
                if frames is not None and polled >= frames:
                    break
                sleep(interval)
    except KeyboardInterrupt:
        pass
