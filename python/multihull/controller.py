from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import grpc

from multihull import discovery, engine
from multihull._proto import discovery_pb2 as pb
from multihull._proto import discovery_pb2_grpc as pb_grpc
from multihull.apikeys import ApiKeyError
from multihull.deploy import DEFAULT_SNAPSHOT_PATH
from multihull.providers.base import Observed, Provider, Ref, ScaleRefused
from multihull.spec import ServiceSpec, TargetSpec
from multihull.state.base import Floor, StateBackend, StateRecord, records_by_provider
from multihull.stream_security import StreamSecurity

log = logging.getLogger("multihull.controller")

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_HEALTH_INTERVAL = timedelta(minutes=5)
DEFAULT_DEGRADED_COOLDOWN = timedelta(minutes=10)
DEFAULT_GRPC_LISTEN = "0.0.0.0:7700"
SCALABLE_PHASES = {"Ready"}
SCALE_BACK_TICKS_PER_COOLDOWN = 10
PERMANENT_SCALE_REFUSALS = (ScaleRefused, NotImplementedError)


class ScaleResult(Enum):
    APPLIED = "applied"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass
class ScaleAction:
    provider: str
    previous_min: int
    new_min: int
    max: int


@dataclass
class ScaleStep(ScaleAction):
    ref: str
    cause: str = ""

    @property
    def raising(self) -> bool:
        return self.new_min > self.previous_min

    @property
    def verb(self) -> str:
        return "scale" if self.raising else "scale back"

    def action(self) -> ScaleAction:
        return ScaleAction(self.provider, self.previous_min, self.new_min, self.max)


@dataclass
class HealthReport:
    provider: str
    credentials_ok: bool
    credentials_message: str
    gpu_offers: int


class Controller:
    def __init__(
        self,
        spec: ServiceSpec,
        state: StateBackend,
        providers: Mapping[str, Provider],
        snapshot_out: str | Path | None = DEFAULT_SNAPSHOT_PATH,
        interval: timedelta = DEFAULT_INTERVAL,
        health_interval: timedelta = DEFAULT_HEALTH_INTERVAL,
        degraded_cooldown: timedelta = DEFAULT_DEGRADED_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.state = state
        self.providers = providers
        self.snapshot_out = Path(snapshot_out) if snapshot_out else None
        self.interval = interval
        self.health_interval = health_interval
        self.degraded_cooldown = degraded_cooldown
        self.clock = clock
        self.version = 0
        self.snapshot: dict[str, Any] | None = None
        self.observed: dict[str, Observed] = {}
        self.min_replicas: dict[str, int] = {
            t.provider: spec.effective_replicas(t).min for t in spec.targets
        }
        self.pre_degraded_min: dict[str, int] = {}
        self.last_degraded_at: float | None = None
        self.degraded_signals = 0
        self.last_scale_back_at: float | None = None
        self.scaling = threading.Lock()
        self.scaling_targets: set[str] = set()
        self.owed_raises: dict[str, str] = {}
        self.restore_floors()
        self.subscribers: set[asyncio.Queue[pb.Snapshot | None]] = set()
        self.acked_versions: dict[str, int] = {}
        self.scale_actions: list[ScaleAction] = []
        self.scale_back_actions: list[ScaleAction] = []

    def reconcile_sync(self) -> bool:
        results = engine.refresh(self.spec.name, self.state, self.providers)
        self.observed = {r.provider: r.observed for r in results}
        candidate = discovery.build_snapshot(
            self.spec, self.state, self.providers, observed=self.observed, version=self.version + 1
        )
        if not discovery.snapshot_changed(self.snapshot, candidate):
            return False
        self.version += 1
        self.snapshot = candidate
        if self.snapshot_out is not None:
            discovery.write_snapshot(candidate, self.snapshot_out)
        log.info("snapshot version %d: %s", self.version, self.describe_endpoints())
        return True

    def describe_endpoints(self) -> str:
        if self.snapshot is None:
            return "no snapshot"
        entries = [
            f"{e['provider']}={e['health']}/{e['ready_replicas']}"
            for route in self.snapshot["routes"]
            for e in route["endpoints"]
        ]
        return ", ".join(entries) or "no endpoints"

    async def reconcile_once(self) -> bool:
        changed = await asyncio.to_thread(self.reconcile_sync)
        if changed:
            self.publish()
        return changed

    def current_proto(self) -> pb.Snapshot | None:
        return discovery.snapshot_to_proto(self.snapshot) if self.snapshot else None

    def publish(self) -> None:
        message = self.current_proto()
        if message is None:
            return
        for queue in list(self.subscribers):
            queue.put_nowait(message)

    def subscribe(self) -> asyncio.Queue[pb.Snapshot | None]:
        queue: asyncio.Queue[pb.Snapshot | None] = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[pb.Snapshot | None]) -> None:
        self.subscribers.discard(queue)

    def record_ack(self, node_id: str, version: int) -> None:
        self.acked_versions[node_id] = version
        log.debug("ack from %s for version %d", node_id, version)

    def check_health_sync(self) -> list[HealthReport]:
        reports: list[HealthReport] = []
        for target in self.spec.targets:
            provider = self.providers[target.provider]
            try:
                credentials = provider.credentials_health()
            except Exception as exc:
                credentials_ok, credentials_message = False, f"check failed: {exc}"
            else:
                credentials_ok, credentials_message = credentials.ok, credentials.message
            try:
                offers = len(provider.gpu_inventory())
            except Exception as exc:
                offers = 0
                log.warning("gpu inventory failed for %s: %s", target.provider, exc)
            reports.append(
                HealthReport(target.provider, credentials_ok, credentials_message, offers)
            )
            level = logging.INFO if credentials_ok else logging.WARNING
            log.log(
                level,
                "health %s: credentials %s (%s), %d gpu offers",
                target.provider,
                "ok" if credentials_ok else "failed",
                credentials_message,
                offers,
            )
        return reports

    async def check_health_once(self) -> list[HealthReport]:
        return await asyncio.to_thread(self.check_health_sync)

    def restore_floors(self) -> None:
        for floor in self.state.list_floors(self.spec.name):
            if floor.provider not in self.min_replicas:
                continue
            replicas = self.spec.effective_replicas(self.target(floor.provider))
            self.min_replicas[floor.provider] = min(
                max(floor.min_replicas, replicas.min), replicas.max
            )
            if floor.pre_degraded_min is not None:
                self.pre_degraded_min[floor.provider] = floor.pre_degraded_min
        if self.pre_degraded_min:
            self.last_degraded_at = self.clock()
            log.info(
                "restored raised floors %s; scale-back resumes after %s without degraded",
                self.min_replicas,
                self.degraded_cooldown,
            )

    def persist_floor(self, provider: str) -> None:
        minimum = self.min_replicas[provider]
        remembered = self.pre_degraded_min.get(provider)
        spec_min = self.spec.effective_replicas(self.target(provider)).min
        try:
            if remembered is None and minimum == spec_min:
                self.state.delete_floor(self.spec.name, provider)
            else:
                self.state.put_floor(Floor(self.spec.name, provider, minimum, remembered))
        except Exception as exc:
            log.error("saving the min replicas floor of %s failed: %s", provider, exc)

    def handle_degraded_sync(self, service: str, provider: str, reason: str) -> list[ScaleAction]:
        if service != self.spec.name:
            log.warning("degraded signal for unknown service %s ignored", service)
            return []
        log.warning("degraded %s/%s: %s", service, provider, reason)
        records = records_by_provider(self.state.list(service))
        steps: list[ScaleStep] = []
        with self.scaling:
            self.last_degraded_at = self.clock()
            self.degraded_signals += 1
            for target in self.spec.targets:
                if target.provider == provider:
                    continue
                if target.provider in self.scaling_targets:
                    self.owed_raises[target.provider] = provider
                    continue
                step = self.plan_raise(target, records, provider)
                if step is not None:
                    steps.append(step)
        return [action for step in steps if (action := self.execute(step, records))]

    def plan_raise(
        self, target: TargetSpec, records: Mapping[str, StateRecord], cause: str
    ) -> ScaleStep | None:
        record = records.get(target.provider)
        if record is None:
            return None
        observed = self.observed.get(target.provider)
        phase = observed.phase if observed else record.last_status
        replicas = self.spec.effective_replicas(target)
        current = self.min_replicas[target.provider]
        scaled_to_zero = current == 0 and phase == "Pending"
        if phase not in SCALABLE_PHASES and not scaled_to_zero:
            return None
        floor = 0 if self.spec.reliability.fallbackScaleToZero else 1
        new_min = max(floor, min(current + 1, replicas.max))
        if new_min == current:
            return None
        return self.claim(
            ScaleStep(target.provider, current, new_min, replicas.max, record.ref, cause)
        )

    def plan_step_down(
        self, target: TargetSpec, records: Mapping[str, StateRecord]
    ) -> ScaleStep | None:
        remembered = self.pre_degraded_min.get(target.provider)
        if remembered is None or target.provider in self.scaling_targets:
            return None
        replicas = self.spec.effective_replicas(target)
        current = self.min_replicas[target.provider]
        if current <= max(remembered, replicas.min):
            del self.pre_degraded_min[target.provider]
            self.persist_floor(target.provider)
            return None
        new_min = current - 1
        if new_min < 1 and self.warm_targets() - 1 < self.spec.reliability.minWarmProviders:
            log.info(
                "keeping %s at min=%d: reliability.minWarmProviders is %d",
                target.provider,
                current,
                self.spec.reliability.minWarmProviders,
            )
            del self.pre_degraded_min[target.provider]
            self.persist_floor(target.provider)
            return None
        record = records.get(target.provider)
        if record is None:
            return None
        return self.claim(ScaleStep(target.provider, current, new_min, replicas.max, record.ref))

    def claim(self, step: ScaleStep) -> ScaleStep:
        self.scaling_targets.add(step.provider)
        return step

    def execute(self, step: ScaleStep, records: Mapping[str, StateRecord]) -> ScaleAction | None:
        committed, owed = self.apply_and_commit(step, records)
        while owed is not None:
            _, owed = self.apply_and_commit(owed, records)
        return committed

    def apply_and_commit(
        self, step: ScaleStep, records: Mapping[str, StateRecord]
    ) -> tuple[ScaleAction | None, ScaleStep | None]:
        result = self.apply_scale(step.verb, step.provider, step.ref, step.new_min, step.max)
        with self.scaling:
            committed = None if result is ScaleResult.FAILED else self.commit(step, result)
            self.scaling_targets.discard(step.provider)
            cause = self.owed_raises.pop(step.provider, None)
            if cause is None:
                return committed, None
            return committed, self.plan_raise(self.target(step.provider), records, cause)

    def commit(self, step: ScaleStep, result: ScaleResult) -> ScaleAction:
        action = step.action()
        self.min_replicas[step.provider] = step.new_min
        if step.raising:
            if result is ScaleResult.APPLIED:
                log.info(
                    "scaled %s min replicas %d -> %d (max %d) after degraded %s",
                    step.provider,
                    step.previous_min,
                    step.new_min,
                    step.max,
                    step.cause,
                )
            self.pre_degraded_min.setdefault(step.provider, step.previous_min)
            self.persist_floor(step.provider)
            self.scale_actions.append(action)
            return action
        if result is ScaleResult.APPLIED:
            log.info(
                "scaled back %s min replicas %d -> %d (max %d): no degraded for %s",
                step.provider,
                step.previous_min,
                step.new_min,
                step.max,
                self.degraded_cooldown,
            )
        replicas = self.spec.effective_replicas(self.target(step.provider))
        remembered = self.pre_degraded_min.get(step.provider, replicas.min)
        if step.new_min <= max(remembered, replicas.min):
            self.pre_degraded_min.pop(step.provider, None)
        self.persist_floor(step.provider)
        self.scale_back_actions.append(action)
        return action

    def target(self, provider: str) -> TargetSpec:
        return next(t for t in self.spec.targets if t.provider == provider)

    def apply_scale(
        self, verb: str, provider: str, ref: str, new_min: int, maximum: int
    ) -> ScaleResult:
        try:
            self.providers[provider].scale(Ref.from_json(ref), new_min, maximum)
        except PERMANENT_SCALE_REFUSALS as exc:
            log.warning(
                "%s %s to min=%d failed: refused by provider, recorded as intent: %s",
                verb,
                provider,
                new_min,
                exc,
            )
            return ScaleResult.REFUSED
        except Exception as exc:
            log.error(
                "%s %s to min=%d failed: %s; keeping the current min and retrying later",
                verb,
                provider,
                new_min,
                exc,
            )
            return ScaleResult.FAILED
        return ScaleResult.APPLIED

    async def handle_degraded(self, message: pb.Degraded) -> list[ScaleAction]:
        reason = pb.DegradedReason.Name(message.reason)
        return await asyncio.to_thread(
            self.handle_degraded_sync, message.service, message.provider, reason
        )

    def warm_targets(self) -> int:
        return sum(1 for minimum in self.min_replicas.values() if minimum >= 1)

    def scale_back_due(self, now: float) -> bool:
        if not self.pre_degraded_min or self.last_degraded_at is None:
            return False
        cooldown = self.degraded_cooldown.total_seconds()
        last_step = self.last_scale_back_at
        quiet_since = (
            self.last_degraded_at if last_step is None else max(last_step, self.last_degraded_at)
        )
        return now - quiet_since >= cooldown

    def scale_back_sync(self, now: float | None = None) -> list[ScaleAction]:
        now = self.clock() if now is None else now
        with self.scaling:
            if not self.scale_back_due(now):
                return []
            signals = self.degraded_signals
        records = records_by_provider(self.state.list(self.spec.name))
        actions: list[ScaleAction] = []
        for target in self.spec.targets:
            with self.scaling:
                if self.degraded_signals != signals:
                    log.info("scale back stopped: a degraded signal arrived during the step")
                    break
                step = self.plan_step_down(target, records)
            if step is not None and (action := self.execute(step, records)):
                actions.append(action)
        with self.scaling:
            self.last_scale_back_at = now
        return actions

    async def scale_back_once(self) -> list[ScaleAction]:
        return await asyncio.to_thread(self.scale_back_sync)

    async def reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except ApiKeyError as exc:
                log.error("snapshot not published: %s", exc)
            except Exception:
                log.exception("reconcile failed")
            await asyncio.sleep(self.interval.total_seconds())

    async def health_loop(self) -> None:
        while True:
            try:
                await self.check_health_once()
            except Exception:
                log.exception("health check failed")
            await asyncio.sleep(self.health_interval.total_seconds())

    def scale_back_tick(self) -> float:
        return max(1.0, self.degraded_cooldown.total_seconds() / SCALE_BACK_TICKS_PER_COOLDOWN)

    async def scale_back_loop(self) -> None:
        while True:
            await asyncio.sleep(self.scale_back_tick())
            try:
                await self.scale_back_once()
            except Exception:
                log.exception("scale back failed")

    async def serve(self, listen: str, security: StreamSecurity) -> tuple[grpc.aio.Server, int]:
        security.validate()
        server = grpc.aio.server()
        pb_grpc.add_DiscoveryServicer_to_server(DiscoveryServicer(self, security), server)
        if security.insecure:
            port = server.add_insecure_port(listen)
            log.warning(
                "discovery stream is plaintext%s (--insecure); snapshots can carry provider "
                "credentials, so use --tls-cert and --tls-key outside local development",
                "" if security.token else " and unauthenticated",
            )
        else:
            port = server.add_secure_port(listen, security.server_credentials())
        await server.start()
        log.info(
            "discovery stream listening on %s (%s)",
            listen if port == 0 else f"port {port}",
            security.describe(),
        )
        return server, port

    async def run(self, security: StreamSecurity, listen: str = DEFAULT_GRPC_LISTEN) -> None:
        server, _ = await self.serve(listen, security)
        loops = [
            asyncio.create_task(self.reconcile_loop()),
            asyncio.create_task(self.health_loop()),
            asyncio.create_task(self.scale_back_loop()),
        ]
        try:
            await server.wait_for_termination()
        finally:
            for task in loops:
                task.cancel()
            await server.stop(grace=5)


class DiscoveryServicer(pb_grpc.DiscoveryServicer):
    def __init__(self, controller: Controller, security: StreamSecurity) -> None:
        self.controller = controller
        self.security = security

    async def Stream(
        self, request_iterator: AsyncIterator[pb.RouterMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.ControlMessage]:
        problem = self.security.authorization_problem(context.invocation_metadata())
        if problem is not None:
            log.warning("rejected discovery stream from %s: %s", context.peer(), problem)
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, problem)
        queue = self.controller.subscribe()
        node = {"id": context.peer()}

        async def consume() -> None:
            try:
                async for message in request_iterator:
                    await self.handle(message, queue, node)
            finally:
                queue.put_nowait(None)

        reader = asyncio.create_task(consume())
        try:
            while (snapshot := await queue.get()) is not None:
                yield pb.ControlMessage(snapshot=snapshot)
        finally:
            reader.cancel()
            self.controller.unsubscribe(queue)

    async def handle(
        self,
        message: pb.RouterMessage,
        queue: asyncio.Queue[pb.Snapshot | None],
        node: dict[str, str],
    ) -> None:
        kind = message.WhichOneof("message")
        if kind == "hello":
            node["id"] = message.hello.node_id or node["id"]
            log.info("hello from %s (last version %d)", node["id"], message.hello.last_version)
            snapshot = self.controller.current_proto()
            if snapshot is None:
                await self.controller.reconcile_once()
                return
            queue.put_nowait(snapshot)
        elif kind == "ack":
            self.controller.record_ack(node["id"], message.ack.version)
        elif kind == "nack":
            log.warning(
                "nack from %s for version %d: %s",
                node["id"],
                message.nack.version,
                message.nack.reason,
            )
        elif kind == "degraded":
            await self.controller.handle_degraded(message.degraded)
