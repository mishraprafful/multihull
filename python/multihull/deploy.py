from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from multihull import discovery, engine
from multihull.providers.base import Observed, Provider, Ref
from multihull.spec import ServiceSpec
from multihull.state.base import StateBackend

DEFAULT_SNAPSHOT_PATH = Path(".multihull/snapshot.json")
READY_TIMEOUTS: dict[str, timedelta] = {
    "kubernetes": timedelta(minutes=10),
    "modal": timedelta(minutes=15),
    "runpod": timedelta(minutes=15),
    "baseten": timedelta(minutes=20),
    "replicate": timedelta(minutes=20),
    "docker": timedelta(minutes=2),
}
FALLBACK_TIMEOUT = timedelta(minutes=15)
TERMINAL_PHASES = {"Ready", "Failed"}


@dataclass
class TargetOutcome:
    provider: str
    type: str
    change: str
    ok: bool
    phase: str = "Unknown"
    ready_replicas: int = 0
    desired_replicas: int = 0
    message: str = ""
    url: str | None = None


@dataclass
class DeployReport:
    service: str
    dry_run: bool
    outcomes: list[TargetOutcome]
    snapshot: dict[str, Any] | None = None
    snapshot_path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def failed(self) -> list[TargetOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]


def ready_timeout(provider_type: str, override: timedelta | None) -> timedelta:
    if override is not None:
        return override
    return READY_TIMEOUTS.get(provider_type, FALLBACK_TIMEOUT)


def wait_ready(
    provider: Provider,
    ref: Ref,
    timeout: timedelta,
    poll_interval: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Observed:
    deadline = clock() + timeout.total_seconds()
    observed = Observed(phase="Unknown", message="not polled")
    while True:
        try:
            observed = provider.status(ref)
        except Exception as exc:
            observed = Observed(phase="Unknown", message=str(exc))
        if observed.phase in TERMINAL_PHASES:
            return observed
        if clock() >= deadline:
            return Observed(
                phase=observed.phase,
                ready_replicas=observed.ready_replicas,
                desired_replicas=observed.desired_replicas,
                message=f"timed out after {timeout} waiting for Ready ({observed.message})".strip(),
            )
        sleep(poll_interval)


def endpoint_url(provider: Provider, ref: Ref) -> str | None:
    try:
        return provider.endpoint(ref).url
    except Exception:
        return None


def deploy(
    spec: ServiceSpec,
    state: StateBackend,
    providers: Mapping[str, Provider],
    dry_run: bool = True,
    only: Collection[str] | None = None,
    wait: bool = True,
    timeout: timedelta | None = None,
    poll_interval: float = 5.0,
    snapshot_out: str | Path | None = DEFAULT_SNAPSHOT_PATH,
    image_digest: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DeployReport:
    results = engine.apply(
        spec,
        state,
        dry_run=dry_run,
        providers=providers,
        image_digest=image_digest,
        only=only,
    )
    if dry_run:
        outcomes = [
            TargetOutcome(r.provider, r.type, r.change, r.ok, message=r.message) for r in results
        ]
        return DeployReport(spec.name, True, outcomes)

    applied = [
        r for r in results if r.ok and r.ref is not None and r.change in engine.APPLIED_CHANGES
    ]

    def observe(result: engine.ApplyResult) -> Observed:
        provider = providers[result.provider]
        ref = result.ref
        assert ref is not None
        if not wait:
            try:
                return provider.status(ref)
            except Exception as exc:
                return Observed(phase="Unknown", message=str(exc))
        return wait_ready(
            provider, ref, ready_timeout(result.type, timeout), poll_interval, sleep=sleep
        )

    with ThreadPoolExecutor(max_workers=max(len(applied), 1)) as pool:
        observations = dict(
            zip((r.provider for r in applied), pool.map(observe, applied), strict=True)
        )

    with state.lock():
        for result in applied:
            record = state.get(spec.name, result.provider)
            if record is not None:
                record.last_status = observations[result.provider].phase
                state.put(record)

    outcomes: list[TargetOutcome] = []
    for result in results:
        observed = observations.get(result.provider)
        if observed is None:
            outcomes.append(
                TargetOutcome(
                    result.provider, result.type, result.change, result.ok, message=result.message
                )
            )
            continue
        ready = observed.phase == "Ready" or (not wait and observed.phase != "Failed")
        outcomes.append(
            TargetOutcome(
                result.provider,
                result.type,
                result.change,
                ready,
                observed.phase,
                observed.ready_replicas,
                observed.desired_replicas,
                observed.message or result.message,
                endpoint_url(providers[result.provider], result.ref) if result.ref else None,
            )
        )

    snapshot = discovery.file_snapshot(spec, state, providers, snapshot_out, observed=observations)
    snapshot_path = discovery.write_snapshot(snapshot, snapshot_out) if snapshot_out else None
    return DeployReport(spec.name, False, outcomes, snapshot, snapshot_path)
