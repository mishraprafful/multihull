from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import grpc

from multihull._proto import discovery_pb2 as pb
from multihull._proto import discovery_pb2_grpc as pb_grpc
from multihull.controller import Controller
from multihull.providers.base import Ref
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord
from tests.fakes import FakeProvider


def seed_state(spec: ServiceSpec, state: LocalState, status: str = "Pending") -> None:
    for target in spec.targets:
        ref = Ref(target.provider, target.type, spec.name, {"id": "1"})
        state.put(StateRecord(spec.name, target.provider, ref.to_json(), None, "h", status))


def make_controller(
    spec: ServiceSpec, tmp_path: Path, providers: dict[str, FakeProvider]
) -> tuple[Controller, LocalState]:
    state = LocalState(tmp_path / "state.db")
    seed_state(spec, state)
    controller = Controller(
        spec,
        state,
        providers,
        snapshot_out=tmp_path / "snapshot.json",
        interval=timedelta(seconds=60),
    )
    return controller, state


def test_reconcile_tick_updates_state_and_versions(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    providers = {t.provider: FakeProvider(phases=["Pending", "Ready"]) for t in llama_spec.targets}
    controller, state = make_controller(llama_spec, tmp_path, providers)

    async def scenario() -> None:
        assert await controller.reconcile_once() is True
        assert controller.version == 1
        health = {e["provider"]: e["health"] for e in controller.snapshot["routes"][0]["endpoints"]}
        assert health == {"gke-prod": "unknown", "modal-main": "unknown", "runpod-eu": "unknown"}

        assert await controller.reconcile_once() is True
        assert controller.version == 2
        assert {r.last_status for r in state.list(llama_spec.name)} == {"Ready"}
        written = json.loads((tmp_path / "snapshot.json").read_text())
        assert written["version"] == 2
        assert {e["health"] for e in written["routes"][0]["endpoints"]} == {"healthy"}

        assert await controller.reconcile_once() is False
        assert controller.version == 2

        reports = await controller.check_health_once()
        assert [r.provider for r in reports] == ["gke-prod", "modal-main", "runpod-eu"]
        assert all(r.credentials_ok for r in reports)

    asyncio.run(scenario())


def test_degraded_scales_healthy_targets_up_to_max(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    controller, _ = make_controller(llama_spec, tmp_path, providers)

    async def scenario() -> None:
        await controller.reconcile_once()
        signal = pb.Degraded(
            service="llama-8b", provider="gke-prod", reason=pb.DEGRADED_REASON_QUEUE_DEPTH
        )
        for _ in range(10):
            await controller.handle_degraded(signal)
        assert providers["gke-prod"].scaled == []
        assert providers["modal-main"].scaled[0] == ("modal-main", 2, 8)
        assert providers["modal-main"].scaled[-1] == ("modal-main", 8, 8)
        assert len(providers["modal-main"].scaled) == 7
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 8, "runpod-eu": 8}

        unknown = pb.Degraded(service="other", provider="gke-prod")
        assert await controller.handle_degraded(unknown) == []

    asyncio.run(scenario())


def test_degraded_wakes_scaled_to_zero_fallback(llama_raw: dict, tmp_path: Path) -> None:
    llama_raw["reliability"]["fallbackScaleToZero"] = True
    llama_raw["targets"][2]["replicas"] = {"min": 0, "max": 2}
    spec = ServiceSpec.model_validate(llama_raw)
    providers = {
        "gke-prod": FakeProvider(),
        "modal-main": FakeProvider(),
        "runpod-eu": FakeProvider(phases=["Pending"]),
    }
    controller, _ = make_controller(spec, tmp_path, providers)

    async def scenario() -> None:
        await controller.reconcile_once()
        signal = pb.Degraded(service="llama-8b", provider="modal-main")
        actions = await controller.handle_degraded(signal)
        assert {(a.provider, a.previous_min, a.new_min) for a in actions} == {
            ("gke-prod", 2, 3),
            ("runpod-eu", 0, 1),
        }
        assert providers["runpod-eu"].scaled == [("runpod-eu", 1, 2)]

    asyncio.run(scenario())


def test_grpc_stream_hello_snapshot_and_degraded(llama_spec: ServiceSpec, tmp_path: Path) -> None:
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    controller, _ = make_controller(llama_spec, tmp_path, providers)

    async def scenario() -> None:
        server, port = await controller.serve("127.0.0.1:0")
        outgoing: asyncio.Queue[pb.RouterMessage | None] = asyncio.Queue()

        async def requests() -> AsyncIterator[pb.RouterMessage]:
            while (message := await outgoing.get()) is not None:
                yield message

        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.DiscoveryStub(channel)
            call = stub.Stream(requests())
            await outgoing.put(pb.RouterMessage(hello=pb.Hello(node_id="router-1")))
            first = await asyncio.wait_for(call.read(), timeout=5)
            assert first.WhichOneof("message") == "snapshot"
            assert first.snapshot.version == 1
            endpoints = first.snapshot.routes[0].endpoints
            assert [e.provider for e in endpoints] == ["gke-prod", "modal-main", "runpod-eu"]
            assert endpoints[0].health == pb.HEALTH_READY

            await outgoing.put(pb.RouterMessage(ack=pb.Ack(version=1)))
            await outgoing.put(pb.RouterMessage(nack=pb.Nack(version=1, reason="test")))
            await outgoing.put(
                pb.RouterMessage(
                    degraded=pb.Degraded(
                        service="llama-8b",
                        provider="gke-prod",
                        reason=pb.DEGRADED_REASON_TTFT_P95,
                        observed_concurrency=40,
                    )
                )
            )
            for _ in range(100):
                if len(controller.scale_actions) == 2 and controller.acked_versions:
                    break
                await asyncio.sleep(0.02)
            assert providers["modal-main"].scaled == [("modal-main", 2, 8)]
            assert providers["runpod-eu"].scaled == [("runpod-eu", 2, 8)]
            assert providers["gke-prod"].scaled == []
            assert controller.acked_versions == {"router-1": 1}

            providers["modal-main"].phases = ["Degraded"]
            assert await controller.reconcile_once() is True
            second = await asyncio.wait_for(call.read(), timeout=5)
            assert second.snapshot.version == 2
            assert second.snapshot.routes[0].endpoints[1].health == pb.HEALTH_DEGRADED

            await outgoing.put(None)
            assert await asyncio.wait_for(call.read(), timeout=5) is grpc.aio.EOF
        assert controller.subscribers == set()
        await server.stop(None)

    asyncio.run(scenario())
