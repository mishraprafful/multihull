from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from live import demo
from live.demo import (
    DEFAULT_SPEC,
    DemoArgs,
    api_key_env,
    kubectl_moments,
    modal_moments,
    model_name,
    parse_args,
    ready_timeout,
    router_tuning,
    service_name,
)
from live.gpu import ROUTER_TUNING as GPU_ROUTER_TUNING
from live.harness import ROUTER_TUNING, Kind, Settings
from live.sweep import stale_apps
from multihull.providers.base import Ref

GPU_SPEC = DEFAULT_SPEC.parent / "gpu.yaml"


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args == DemoArgs(router_bin=args.router_bin, cluster=args.cluster, image=args.image)
    assert args.spec == DEFAULT_SPEC


@pytest.mark.parametrize(
    "argv",
    [
        ["--scripted"],
        ["--scripted", "--duration", "30", "--kill-at", "20", "--restore-at", "50"],
        ["--rate", "0"],
        ["--run-id", "Not_Valid"],
    ],
)
def test_parse_args_rejects_inconsistent_values(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_service_names_fall_under_the_live_sweeper_prefix() -> None:
    service = service_name("ab12")
    app = f"multihull-{service}-modal"
    apps = [
        {
            "app_id": "ap-1",
            "description": app,
            "state": "deployed",
            "created_at": "2026-10-09 10:00:00+00:00",
        }
    ]
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert app.startswith("multihull-live-")
    assert [
        a["description"] for a in stale_apps(apps, f"multihull-{service}", timedelta(0), now)
    ] == [app]


def test_cpu_spec_is_kind_primary_modal_secondary_with_a_route_key() -> None:
    document = load(DEFAULT_SPEC)
    ordered = sorted(document["targets"], key=lambda t: t["priority"])
    assert [(t["provider"], t["type"]) for t in ordered] == [
        ("kind", "kubernetes"),
        ("modal", "modal"),
    ]
    assert document["resources"]["gpu"] == []
    assert "registrySecret" not in ordered[1]["modal"]
    assert api_key_env(document) == "MULTIHULL_DEMO_API_KEYS"
    assert model_name(document) == "mock-llm"
    assert router_tuning(document) == ROUTER_TUNING
    assert ready_timeout(document) == "302s"


def test_gpu_spec_reuses_the_live_gpu_values() -> None:
    document = load(GPU_SPEC)
    live_gpu = load(Path(__file__).resolve().parents[1] / "specs" / "gpu-modal.yaml")
    assert document["container"] == live_gpu["container"]
    assert document["resources"] == live_gpu["resources"]
    assert [t["modal"] for t in document["targets"]] == [t["modal"] for t in live_gpu["targets"]]
    assert api_key_env(document) == "MULTIHULL_DEMO_API_KEYS"
    assert model_name(document) == "qwen2.5-1.5b-instruct"
    assert router_tuning(document) == GPU_ROUTER_TUNING
    assert ready_timeout(document) == "900s"


def test_api_key_env_is_none_without_an_env_source() -> None:
    assert api_key_env({"route": {}}) is None
    assert api_key_env({"route": {"auth": {"apiKeys": {"from": "file:keys"}}}}) is None


def test_kubectl_moments_scale_the_service_deployment(tmp_path: Path) -> None:
    settings = Settings("demo", "live-demo-ab12", "multihull-live", None, tmp_path, None)
    kind = Kind(settings, "multihull-demo", tmp_path / "kubectl.log")
    stop, start = kubectl_moments(kind, "live-demo-ab12", 20.0, 50.0)
    assert (stop.at, start.at) == (20.0, 50.0)
    assert stop.argv == [
        "kubectl",
        "--context",
        "kind-multihull-live",
        "--namespace",
        "multihull-demo",
        "scale",
        "deployment/live-demo-ab12",
        "--replicas=0",
    ]
    assert start.argv[-1] == "--replicas=1"
    assert stop.shown == " ".join(stop.argv)


class FakeDeployment:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    def refs(self) -> dict[str, Ref]:
        ids = {"app": "multihull-live-demo-ab12-modal-a", "environment": "main"}
        return {"modal-a": Ref(provider="modal-a", type="modal", service="live-demo-ab12", ids=ids)}


def test_modal_moments_stop_the_app_and_redeploy_the_target(tmp_path: Path) -> None:
    stop, start = modal_moments(FakeDeployment(tmp_path), "modal-a", 20.0, 50.0)  # type: ignore[arg-type]
    assert stop.shown == "modal app stop multihull-live-demo-ab12-modal-a --env main --yes"
    assert stop.argv[:3] == [sys.executable, "-m", "modal"]
    assert start.argv[:4] == [sys.executable, "-m", "multihull.cli", "deploy"]
    assert "--target" in start.argv and "modal-a" in start.argv and "--no-wait" in start.argv
    assert start.cwd == tmp_path
    assert start.shown.startswith(f"cd {tmp_path} && uv run --project ")
    assert start.shown.endswith("--snapshot-out .multihull/snapshot.json")


def test_demo_never_prints_the_route_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MULTIHULL_DEMO_API_KEYS", raising=False)
    monkeypatch.setattr(demo.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    lines: list[str] = []
    run = demo.CloudDemo(parse_args(["--run-id", "ab12"]), out=lines.append)
    run.prepare_workdir()
    assert run.key_path.read_text().strip() == run.api_key
    assert oct(run.key_path.stat().st_mode & 0o777) == "0o600"
    assert all(run.api_key not in line for line in lines)
    assert run.document["name"] == "live-demo-ab12"
    assert run.document["targets"][0]["kubernetes"]["context"] == "kind-multihull-live"
