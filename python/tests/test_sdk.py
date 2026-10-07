from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from multihull import Service, ServiceSpec, target
from multihull import spec as specmod
from multihull.state import LocalState
from tests.conftest import FIXTURES
from tests.fakes import FakeProvider


def fake_service(tmp_path: Path, **kwargs: Any) -> tuple[Service, dict[str, FakeProvider]]:
    spec = specmod.load(FIXTURES / "llama-8b.yaml")
    providers = {t.provider: FakeProvider() for t in spec.targets}
    service = Service.from_yaml(
        FIXTURES / "llama-8b.yaml",
        state=LocalState(tmp_path / "state.db"),
        snapshot_path=tmp_path / "snapshot.json",
        providers=providers,
        **kwargs,
    )
    return service, providers


def test_from_yaml_matches_kwargs(llama_raw: dict[str, Any]) -> None:
    from_yaml = Service.from_yaml(FIXTURES / "llama-8b.yaml")
    from_kwargs = Service(**llama_raw)
    assert from_yaml.spec == from_kwargs.spec
    assert from_yaml.name == "llama-8b"
    assert [t.provider for t in from_yaml.targets] == ["gke-prod", "modal-main", "runpod-eu"]
    assert from_yaml.to_dict() == specmod.dump(specmod.load(FIXTURES / "llama-8b.yaml"))


def test_target_factory_builds_spec_targets() -> None:
    k8s = target.kubernetes("gke", 1, namespace="inference", context="ctx", replicas=(2, 4))
    assert k8s.type == "kubernetes" and k8s.kubernetes.namespace == "inference"
    assert k8s.kubernetes.context == "ctx" and k8s.replicas.min == 2 and k8s.replicas.max == 4
    assert k8s.kubernetes.serviceType == "LoadBalancer" and k8s.kubernetes.endpoint is None
    kind = target.kubernetes(
        "kind", 2, service_type="NodePort", node_port=30080, endpoint="http://127.0.0.1:30080"
    )
    assert kind.kubernetes.nodePort == 30080 and kind.kubernetes.serviceType == "NodePort"
    modal = target.modal("modal-eu", 2, region="eu", weight=3)
    assert modal.modal.region == "eu" and modal.modal.environment == "main" and modal.weight == 3
    private = target.modal(
        "modal-ghcr",
        8,
        registry_secret={"usernameEnv": "GHCR_USERNAME", "passwordEnv": "GHCR_TOKEN"},
    )
    assert private.modal.registrySecret.usernameEnv == "GHCR_USERNAME"
    runpod = target.runpod("runpod-eu", 3, data_centers=["EU-RO-1"], replicas={"min": 0})
    assert runpod.runpod.dataCenters == ["EU-RO-1"] and runpod.replicas.min == 0
    assert target.baseten("bt", 4).type == "baseten"
    assert target.replicate("rep", 5, owner="acme").replicate.owner == "acme"
    local = target.docker("local", 6, host_port=18001, env={"MOCK": "1"}, pull=False)
    assert local.type == "docker" and local.docker.hostPort == 18001
    assert local.docker.env == {"MOCK": "1"} and local.docker.pull is False
    assert local.docker.host == "127.0.0.1" and local.docker.image is None
    assert target.docker("plain", 7).docker.hostPort is None
    with pytest.raises(ValueError):
        target.kubernetes("bad", 0)


def test_docker_only_service_from_builders(tmp_path: Path) -> None:
    service = Service(
        name="mock-sdk",
        container={"image": "multihull-mock-server:dev", "port": 8000},
        resources={"gpu": []},
        reliability={"minWarmProviders": 2},
        targets=[
            target.docker("docker-a", 1, host_port=18101),
            target.docker("docker-b", 2, host_port=18102),
        ],
        route={"hostname": "mock.localhost"},
        state=LocalState(tmp_path / "state.db"),
        snapshot_path=tmp_path / "snapshot.json",
    )
    plans = service.plan()
    assert [(p.provider, p.type, p.change) for p in plans] == [
        ("docker-a", "docker", "new"),
        ("docker-b", "docker", "new"),
    ]
    assert plans[0].plan.payload["ports"] == {"8000/tcp": ("127.0.0.1", 18101)}


def test_mutating_targets_revalidates(tmp_path: Path) -> None:
    service, providers = fake_service(tmp_path)
    service.targets.append(target.modal("modal-us", priority=4, region="us"))
    providers["modal-us"] = FakeProvider()
    service.provider_overrides["modal-us"] = providers["modal-us"]
    plans = service.plan()
    assert [p.provider for p in plans] == ["gke-prod", "modal-main", "runpod-eu", "modal-us"]
    assert isinstance(service.spec, ServiceSpec) and len(service.spec.targets) == 4

    service.targets.append(target.modal("dupe", priority=4))
    with pytest.raises(ValueError, match="priorities must be unique"):
        service.plan()


def test_deploy_snapshot_destroy_roundtrip(tmp_path: Path) -> None:
    service, providers = fake_service(tmp_path)
    dry = service.deploy()
    assert dry.dry_run and dry.ok and service.state.list() == []

    report = service.deploy(dry_run=False)
    assert report.ok and not report.dry_run
    assert all(p.applied for p in providers.values())
    assert report.snapshot_path == tmp_path / "snapshot.json"
    snapshot = service.snapshot()
    assert [e["provider"] for e in snapshot["routes"][0]["endpoints"]] == [
        "gke-prod",
        "modal-main",
        "runpod-eu",
    ]

    results = service.destroy(target={"runpod-eu"})
    assert [r.provider for r in results] == ["runpod-eu"] and results[0].ok
    assert [r.provider for r in service.state.list("llama-8b")] == ["gke-prod", "modal-main"]
    assert len(service.snapshot()["routes"][0]["endpoints"]) == 2

    assert all(r.ok for r in service.destroy())
    assert service.state.list("llama-8b") == []


def test_to_yaml_roundtrip(tmp_path: Path) -> None:
    service = Service.from_yaml(FIXTURES / "llama-8b.yaml")
    path = service.to_yaml(tmp_path / "out.yaml")
    assert Service.from_yaml(path).spec == service.spec
