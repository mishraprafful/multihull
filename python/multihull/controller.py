from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc

from multihull import discovery, engine
from multihull._proto import discovery_pb2 as pb
from multihull._proto import discovery_pb2_grpc as pb_grpc
from multihull.deploy import DEFAULT_SNAPSHOT_PATH
from multihull.providers.base import Observed, Provider, Ref
from multihull.spec import ServiceSpec
from multihull.state.base import StateBackend

log = logging.getLogger("multihull.controller")

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_HEALTH_INTERVAL = timedelta(minutes=5)
DEFAULT_GRPC_LISTEN = "0.0.0.0:7700"
SCALABLE_PHASES = {"Ready"}


@dataclass
class ScaleAction:
    provider: str
    previous_min: int
    new_min: int
    max: int


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
    ) -> None:
        self.spec = spec
        self.state = state
        self.providers = providers
        self.snapshot_out = Path(snapshot_out) if snapshot_out else None
        self.interval = interval
        self.health_interval = health_interval
        self.version = 0
        self.snapshot: dict[str, Any] | None = None
        self.observed: dict[str, Observed] = {}
        self.min_replicas: dict[str, int] = {
            t.provider: spec.effective_replicas(t).min for t in spec.targets
        }
        self.subscribers: set[asyncio.Queue[pb.Snapshot | None]] = set()
        self.acked_versions: dict[str, int] = {}
        self.scale_actions: list[ScaleAction] = []

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

    def handle_degraded_sync(self, service: str, provider: str, reason: str) -> list[ScaleAction]:
        if service != self.spec.name:
            log.warning("degraded signal for unknown service %s ignored", service)
            return []
        log.warning("degraded %s/%s: %s", service, provider, reason)
        records = {r.provider: r for r in self.state.list(service)}
        floor = 0 if self.spec.reliability.fallbackScaleToZero else 1
        actions: list[ScaleAction] = []
        for target in self.spec.targets:
            if target.provider == provider:
                continue
            record = records.get(target.provider)
            if record is None:
                continue
            observed = self.observed.get(target.provider)
            phase = observed.phase if observed else record.last_status
            replicas = self.spec.effective_replicas(target)
            current = self.min_replicas[target.provider]
            scaled_to_zero = current == 0 and phase == "Pending"
            if phase not in SCALABLE_PHASES and not scaled_to_zero:
                continue
            new_min = max(floor, min(current + 1, replicas.max))
            if new_min == current:
                continue
            try:
                self.providers[target.provider].scale(
                    Ref.from_json(record.ref), new_min, replicas.max
                )
            except Exception as exc:
                log.error("scale %s to min=%d failed: %s", target.provider, new_min, exc)
                continue
            self.min_replicas[target.provider] = new_min
            action = ScaleAction(target.provider, current, new_min, replicas.max)
            actions.append(action)
            self.scale_actions.append(action)
            log.info(
                "scaled %s min replicas %d -> %d (max %d) after degraded %s",
                target.provider,
                current,
                new_min,
                replicas.max,
                provider,
            )
        return actions

    async def handle_degraded(self, message: pb.Degraded) -> list[ScaleAction]:
        reason = pb.DegradedReason.Name(message.reason)
        return await asyncio.to_thread(
            self.handle_degraded_sync, message.service, message.provider, reason
        )

    async def reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
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

    async def serve(self, listen: str) -> tuple[grpc.aio.Server, int]:
        server = grpc.aio.server()
        pb_grpc.add_DiscoveryServicer_to_server(DiscoveryServicer(self), server)
        port = server.add_insecure_port(listen)
        await server.start()
        log.info("discovery stream listening on %s", listen if port == 0 else f"port {port}")
        return server, port

    async def run(self, listen: str = DEFAULT_GRPC_LISTEN) -> None:
        server, _ = await self.serve(listen)
        loops = [
            asyncio.create_task(self.reconcile_loop()),
            asyncio.create_task(self.health_loop()),
        ]
        try:
            await server.wait_for_termination()
        finally:
            for task in loops:
                task.cancel()
            await server.stop(grace=5)


class DiscoveryServicer(pb_grpc.DiscoveryServicer):
    def __init__(self, controller: Controller) -> None:
        self.controller = controller

    async def Stream(
        self, request_iterator: AsyncIterator[pb.RouterMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.ControlMessage]:
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
