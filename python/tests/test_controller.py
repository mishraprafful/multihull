from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest

from multihull._proto import discovery_pb2 as pb
from multihull._proto import discovery_pb2_grpc as pb_grpc
from multihull.controller import Controller
from multihull.providers.base import Ref, ScaleRefused
from multihull.spec import ServiceSpec
from multihull.state import LocalState, StateRecord
from multihull.stream_security import StreamSecurity
from tests.fakes import FakeProvider


def seed_state(spec: ServiceSpec, state: LocalState, status: str = "Pending") -> None:
    for target in spec.targets:
        ref = Ref(target.provider, target.type, spec.name, {"id": "1"})
        state.put(StateRecord(spec.name, target.provider, ref.to_json(), None, "h", status))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def make_controller(
    spec: ServiceSpec,
    tmp_path: Path,
    providers: dict[str, FakeProvider],
    clock: FakeClock | None = None,
    cooldown: timedelta = timedelta(minutes=10),
) -> tuple[Controller, LocalState]:
    state = LocalState(tmp_path / "state.db")
    seed_state(spec, state)
    controller = Controller(
        spec,
        state,
        providers,
        snapshot_out=tmp_path / "snapshot.json",
        interval=timedelta(seconds=60),
        degraded_cooldown=cooldown,
        clock=clock or FakeClock(),
    )
    return controller, state


class RefusingProvider(FakeProvider):
    def __init__(self, refusal: Exception, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refusal = refusal

    def scale(self, ref: Ref, min: int, max: int) -> None:
        self.scaled.append((ref.provider, min, max))
        raise self.refusal


class FlakyProvider(FakeProvider):
    def __init__(self, failures: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.failures = failures

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("provider API timed out")
        super().scale(ref, min, max)


class GatedProvider(FakeProvider):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def scale(self, ref: Ref, min: int, max: int) -> None:
        if self.armed:
            self.armed = False
            self.entered.set()
            assert self.release.wait(timeout=5)
        super().scale(ref, min, max)


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
        assert {e["health"] for e in written["routes"][0]["endpoints"]} == {"ready"}

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


def test_scale_back_steps_down_once_per_cooldown_after_degraded_clears(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    clock = FakeClock()
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    controller, state = make_controller(
        llama_spec, tmp_path, providers, clock=clock, cooldown=timedelta(seconds=60)
    )

    async def scenario() -> None:
        await controller.reconcile_once()
        signal = pb.Degraded(service="llama-8b", provider="gke-prod")
        await controller.handle_degraded(signal)
        clock.advance(30)
        await controller.handle_degraded(signal)
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 3, "runpod-eu": 3}
        assert controller.pre_degraded_min == {"modal-main": 1, "runpod-eu": 1}

        clock.advance(59)
        assert await controller.scale_back_once() == []
        clock.advance(1)
        first = await controller.scale_back_once()
        assert {(a.provider, a.previous_min, a.new_min) for a in first} == {
            ("modal-main", 3, 2),
            ("runpod-eu", 3, 2),
        }
        assert providers["modal-main"].scaled[-1] == ("modal-main", 2, 8)
        assert await controller.scale_back_once() == []

        clock.advance(30)
        await controller.handle_degraded(signal)
        assert controller.min_replicas["modal-main"] == 3
        clock.advance(59)
        assert await controller.scale_back_once() == []
        clock.advance(1)
        assert len(await controller.scale_back_once()) == 2
        clock.advance(60)
        last = await controller.scale_back_once()
        assert {(a.provider, a.new_min) for a in last} == {("modal-main", 1), ("runpod-eu", 1)}
        assert controller.pre_degraded_min == {}
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 1, "runpod-eu": 1}
        assert state.list_floors("llama-8b") == []
        clock.advance(600)
        assert await controller.scale_back_once() == []
        assert len(controller.scale_back_actions) == 6

    asyncio.run(scenario())


def test_degraded_during_a_blocked_scale_back_step_wins(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    clock = FakeClock()
    providers = {t.provider: GatedProvider() for t in llama_spec.targets}
    controller, _ = make_controller(
        llama_spec, tmp_path, providers, clock=clock, cooldown=timedelta(seconds=60)
    )
    asyncio.run(controller.reconcile_once())
    asyncio.run(controller.handle_degraded(pb.Degraded(service="llama-8b", provider="gke-prod")))
    assert controller.min_replicas == {"gke-prod": 2, "modal-main": 2, "runpod-eu": 2}

    clock.advance(60)
    providers["modal-main"].armed = True
    scale_back = threading.Thread(target=controller.scale_back_sync)
    scale_back.start()
    assert providers["modal-main"].entered.wait(timeout=5)
    controller.handle_degraded_sync("llama-8b", "gke-prod", "DEGRADED_REASON_QUEUE_DEPTH")
    providers["modal-main"].release.set()
    scale_back.join(timeout=5)
    assert not scale_back.is_alive()

    last_calls = {name: p.scaled[-1][1] for name, p in providers.items() if p.scaled}
    assert last_calls == {"modal-main": 2, "runpod-eu": 3}
    assert controller.min_replicas == {"gke-prod": 2, "modal-main": 2, "runpod-eu": 3}
    assert controller.pre_degraded_min == {"modal-main": 1, "runpod-eu": 1}
    assert [(a.provider, a.new_min) for a in controller.scale_back_actions] == [("modal-main", 1)]
    clock.advance(59)
    assert controller.scale_back_sync() == []


def test_raised_floors_survive_a_controller_restart(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    controller, _ = make_controller(llama_spec, tmp_path, providers)
    signal = pb.Degraded(service="llama-8b", provider="gke-prod")
    raised = {"gke-prod": 2, "modal-main": 3, "runpod-eu": 3}

    def restart(clock: FakeClock) -> Controller:
        return Controller(
            llama_spec,
            LocalState(tmp_path / "state.db"),
            providers,
            snapshot_out=tmp_path / "snapshot.json",
            degraded_cooldown=timedelta(seconds=60),
            clock=clock,
        )

    async def scenario() -> None:
        await controller.reconcile_once()
        await controller.handle_degraded(signal)
        await controller.handle_degraded(signal)
        assert controller.min_replicas == raised

        clock = FakeClock()
        quiet = restart(clock)
        assert quiet.min_replicas == raised
        assert quiet.pre_degraded_min == {"modal-main": 1, "runpod-eu": 1}
        clock.advance(59)
        assert await quiet.scale_back_once() == []
        clock.advance(1)
        stepped = await quiet.scale_back_once()
        assert {(a.provider, a.previous_min, a.new_min) for a in stepped} == {
            ("modal-main", 3, 2),
            ("runpod-eu", 3, 2),
        }

        busy = restart(FakeClock())
        await busy.reconcile_once()
        calls = {name: len(p.scaled) for name, p in providers.items()}
        await busy.handle_degraded(signal)
        fresh = [call for name, p in providers.items() for call in p.scaled[calls[name] :]]
        assert fresh == [("modal-main", 3, 8), ("runpod-eu", 3, 8)]
        assert busy.pre_degraded_min == {"modal-main": 1, "runpod-eu": 1}

    asyncio.run(scenario())


def test_scale_back_keeps_the_warm_provider_floor(llama_raw: dict, tmp_path: Path) -> None:
    llama_raw["reliability"]["fallbackScaleToZero"] = True
    llama_raw["reliability"]["minWarmProviders"] = 3
    llama_raw["targets"][2]["replicas"] = {"min": 0, "max": 2}
    spec = ServiceSpec.model_validate(llama_raw)
    clock = FakeClock()
    providers = {
        "gke-prod": FakeProvider(),
        "modal-main": FakeProvider(),
        "runpod-eu": FakeProvider(phases=["Pending"]),
    }
    controller, state = make_controller(
        spec, tmp_path, providers, clock=clock, cooldown=timedelta(seconds=10)
    )

    async def scenario() -> None:
        await controller.reconcile_once()
        await controller.handle_degraded(pb.Degraded(service="llama-8b", provider="modal-main"))
        assert controller.min_replicas == {"gke-prod": 3, "modal-main": 1, "runpod-eu": 1}
        clock.advance(10)
        actions = await controller.scale_back_once()
        assert [(a.provider, a.new_min) for a in actions] == [("gke-prod", 2)]
        assert controller.min_replicas["runpod-eu"] == 1
        assert controller.pre_degraded_min == {}
        assert providers["runpod-eu"].scaled == [("runpod-eu", 1, 2)]
        floors = [
            (f.provider, f.min_replicas, f.pre_degraded_min) for f in state.list_floors(spec.name)
        ]
        assert floors == [("runpod-eu", 1, None)]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "refusal",
    [ScaleRefused("runs exactly one container"), NotImplementedError("lands in v0.2")],
    ids=["refused", "not-implemented"],
)
def test_refused_scale_still_records_intent_and_scales_back(
    llama_spec: ServiceSpec, tmp_path: Path, caplog: pytest.LogCaptureFixture, refusal: Exception
) -> None:
    clock = FakeClock()
    providers = {t.provider: RefusingProvider(refusal) for t in llama_spec.targets}
    controller, _ = make_controller(
        llama_spec, tmp_path, providers, clock=clock, cooldown=timedelta(seconds=5)
    )

    async def scenario() -> None:
        await controller.reconcile_once()
        with caplog.at_level("INFO", logger="multihull.controller"):
            await controller.handle_degraded(pb.Degraded(service="llama-8b", provider=""))
            assert controller.min_replicas == {"gke-prod": 3, "modal-main": 2, "runpod-eu": 2}
            clock.advance(5)
            actions = await controller.scale_back_once()
        assert {(a.provider, a.new_min) for a in actions} == {
            ("gke-prod", 2),
            ("modal-main", 1),
            ("runpod-eu", 1),
        }
        assert controller.pre_degraded_min == {}
        messages = [record.getMessage() for record in caplog.records]
        refused_up = [m for m in messages if m.startswith("scale gke-prod to min=3 failed")]
        refused_back = [m for m in messages if m.startswith("scale back gke-prod to min=2 failed")]
        assert refused_up and all("refused" in m and str(refusal) in m for m in refused_up)
        assert refused_back and all("refused" in m for m in refused_back)

    asyncio.run(scenario())


def test_transient_scale_failure_keeps_min_and_retries_on_next_degraded(
    llama_spec: ServiceSpec, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    flaky = FlakyProvider(failures=1)
    providers["modal-main"] = flaky
    controller, _ = make_controller(llama_spec, tmp_path, providers)
    signal = pb.Degraded(service="llama-8b", provider="gke-prod")

    async def scenario() -> None:
        await controller.reconcile_once()
        with caplog.at_level("INFO", logger="multihull.controller"):
            actions = await controller.handle_degraded(signal)
        assert [(a.provider, a.new_min) for a in actions] == [("runpod-eu", 2)]
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 1, "runpod-eu": 2}
        assert controller.pre_degraded_min == {"runpod-eu": 1}
        assert flaky.scaled == []
        failed = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("scale modal-main to min=2 failed")
        ]
        assert failed and all("refused" not in m for m in failed)

        retried = await controller.handle_degraded(signal)
        assert ("modal-main", 1, 2) in {(a.provider, a.previous_min, a.new_min) for a in retried}
        assert flaky.scaled == [("modal-main", 2, 8)]
        assert controller.min_replicas["modal-main"] == 2
        assert controller.pre_degraded_min["modal-main"] == 1

    asyncio.run(scenario())


def test_transient_scale_back_failure_keeps_min_and_retries_next_cooldown(
    llama_spec: ServiceSpec, tmp_path: Path
) -> None:
    clock = FakeClock()
    providers = {t.provider: FakeProvider() for t in llama_spec.targets}
    flaky = FlakyProvider()
    providers["modal-main"] = flaky
    controller, _ = make_controller(
        llama_spec, tmp_path, providers, clock=clock, cooldown=timedelta(seconds=60)
    )

    async def scenario() -> None:
        await controller.reconcile_once()
        await controller.handle_degraded(pb.Degraded(service="llama-8b", provider="gke-prod"))
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 2, "runpod-eu": 2}
        flaky.failures = 1
        clock.advance(60)
        actions = await controller.scale_back_once()
        assert [(a.provider, a.new_min) for a in actions] == [("runpod-eu", 1)]
        assert controller.min_replicas["modal-main"] == 2
        assert controller.pre_degraded_min == {"modal-main": 1}
        clock.advance(60)
        actions = await controller.scale_back_once()
        assert [(a.provider, a.new_min) for a in actions] == [("modal-main", 1)]
        assert flaky.scaled[-1] == ("modal-main", 1, 8)
        assert controller.pre_degraded_min == {}

    asyncio.run(scenario())


def test_failed_raise_never_scales_the_warm_fallback_to_zero(
    llama_raw: dict, tmp_path: Path
) -> None:
    llama_raw["reliability"]["fallbackScaleToZero"] = True
    llama_raw["reliability"]["minWarmProviders"] = 2
    llama_raw["targets"][1]["replicas"] = {"min": 0, "max": 2}
    llama_raw["targets"][2]["replicas"] = {"min": 0, "max": 2}
    spec = ServiceSpec.model_validate(llama_raw)
    clock = FakeClock()
    runpod = FlakyProvider(failures=1, phases=["Pending"])
    providers = {
        "gke-prod": FakeProvider(),
        "modal-main": FakeProvider(phases=["Pending"]),
        "runpod-eu": runpod,
    }
    controller, _ = make_controller(
        spec, tmp_path, providers, clock=clock, cooldown=timedelta(seconds=10)
    )

    async def scenario() -> None:
        await controller.reconcile_once()
        await controller.handle_degraded(pb.Degraded(service="llama-8b", provider="gke-prod"))
        assert providers["modal-main"].scaled == [("modal-main", 1, 2)]
        assert runpod.scaled == []
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 1, "runpod-eu": 0}
        for _ in range(5):
            clock.advance(10)
            await controller.scale_back_once()
        assert ("modal-main", 0, 2) not in providers["modal-main"].scaled
        assert controller.min_replicas == {"gke-prod": 2, "modal-main": 1, "runpod-eu": 0}
        assert controller.warm_targets() == 2

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
        server, port = await controller.serve("127.0.0.1:0", StreamSecurity(insecure=True))
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
