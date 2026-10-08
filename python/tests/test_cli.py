from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from multihull import spec as specmod
from multihull.cli import app
from multihull.controller import Controller
from multihull.providers import docker as dockermod
from multihull.providers.base import Ref
from multihull.state import LocalState, StateRecord
from multihull.stream_security import StreamSecurity
from tests.conftest import FIXTURES, no_keys_message
from tests.fake_docker import FakeDockerClient
from tests.fake_modal import FakeAuthError, FakeModal
from tests.fakes import FakeProvider

runner = CliRunner()


def copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "llama-8b.yaml", dest)
    return dest


def test_validate_ok(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(copy_fixture(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "llama-8b" in result.output


def test_validate_warns_when_fallbacks_cannot_absorb_the_primary(tmp_path: Path) -> None:
    path = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "mock-kind-modal.yaml", path)
    result = runner.invoke(app, ["validate", str(path)], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "warning" in result.output and "raise replicas.max on a fallback" in result.output
    clean = runner.invoke(app, ["validate", str(copy_fixture(tmp_path))])
    assert "warning" not in clean.output


def test_validate_rejects_bad_spec(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: multihull/v1\nname: x\n")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert runner.invoke(app, ["validate", str(tmp_path / "none.yaml")]).exit_code == 2


def test_schema_outputs_json(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "ServiceSpec"
    out = tmp_path / "schema.json"
    assert runner.invoke(app, ["schema", "--out", str(out)]).exit_code == 0
    assert json.loads(out.read_text())["$schema"]


def test_plan_writes_dir(tmp_path: Path) -> None:
    path = copy_fixture(tmp_path)
    out = tmp_path / "plan"
    result = runner.invoke(
        app, ["plan", str(path), "--out", str(out), "--state", str(tmp_path / "state.db")]
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "gke-prod.yaml",
        "modal-main.json",
        "runpod-eu.json",
    ]
    assert "new" in result.output


def test_init_with_and_without_dockerfile(tmp_path: Path) -> None:
    project = tmp_path / "My_Model"
    project.mkdir()
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 0, result.output
    written = yaml.safe_load((project / "multihull.yaml").read_text())
    assert written["name"] == "my-model"
    assert written["container"]["image"].startswith("ghcr.io/")
    specmod.load(project / "multihull.yaml")

    assert runner.invoke(app, ["init", str(project)]).exit_code == 1
    (project / "Dockerfile").write_text("FROM scratch\n")
    result = runner.invoke(app, ["init", str(project), "--force", "--name", "custom"])
    assert result.exit_code == 0, result.output
    written = yaml.safe_load((project / "multihull.yaml").read_text())
    assert written["name"] == "custom"
    assert written["container"]["build"] == {"context": ".", "target": "docker"}
    assert "image" not in written["container"]


def test_status_reads_state(tmp_path: Path) -> None:
    path = copy_fixture(tmp_path)
    state_path = tmp_path / "state.db"
    empty = runner.invoke(app, ["status", str(path), "--state", str(state_path)])
    assert empty.exit_code == 0 and "no state" in empty.output
    state = LocalState(state_path)
    ref = Ref("gke-prod", "kubernetes", "llama-8b", {"namespace": "inference"})
    state.put(StateRecord("llama-8b", "gke-prod", ref.to_json(), None, "h", "Ready"))
    result = runner.invoke(app, ["status", str(path), "--state", str(state_path)])
    assert result.exit_code == 0, result.output
    assert "gke-prod" in result.output and "Ready" in result.output


def test_status_rebuilds_missing_state_via_rediscover(
    tmp_path: Path, fake_registry: dict[str, FakeProvider]
) -> None:
    path = copy_fixture(tmp_path)
    state_path = tmp_path / "state.db"
    fake = fake_registry["kubernetes"]
    fake.rediscovered = Ref("gke-prod", "kubernetes", "llama-8b", {"namespace": "inference"})
    result = runner.invoke(app, ["status", str(path), "--state", str(state_path)])
    assert result.exit_code == 0, result.output
    assert "rebuilt state from rediscover: gke-prod" in result.output
    assert "gke-prod" in result.output and "Ready" in result.output
    records = {r.provider: r for r in LocalState(state_path).list("llama-8b")}
    assert set(records) == {"gke-prod"}
    assert records["gke-prod"].spec_hash and records["gke-prod"].last_status == "Ready"


def test_snapshot_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MODAL_PROXY_TOKEN_ID", raising=False)
    path = copy_fixture(tmp_path)
    state_path = tmp_path / "state.db"
    state = LocalState(state_path)
    ref = Ref(
        "modal-main", "modal", "llama-8b", {"app": "multihull-llama-8b", "environment": "main"}
    )
    state.put(StateRecord("llama-8b", "modal-main", ref.to_json(), None, "h", "Ready"))
    out = tmp_path / "snapshot.json"
    result = runner.invoke(
        app, ["snapshot", str(path), "--out", str(out), "--state", str(state_path)]
    )
    assert result.exit_code == 0, result.output
    snapshot = json.loads(out.read_text())
    assert snapshot["routes"][0]["endpoints"][0]["provider"] == "modal-main"


def test_snapshot_command_never_writes_a_lower_version(tmp_path: Path) -> None:
    path = copy_fixture(tmp_path)
    out = tmp_path / "snapshot.json"
    ahead = 10**12
    out.write_text(json.dumps({"version": ahead, "routes": []}))
    args = ["snapshot", str(path), "--out", str(out), "--state", str(tmp_path / "state.db")]

    assert runner.invoke(app, args).exit_code == 0
    first = json.loads(out.read_text())["version"]
    assert first > ahead
    assert runner.invoke(app, args).exit_code == 0
    assert json.loads(out.read_text())["version"] > first


def test_snapshot_command_rejects_a_malformed_route_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLAMA_API_KEYS", "hull_fixture_one,hull_bad-id_leakedsecretvalue")
    out = tmp_path / "snapshot.json"
    result = runner.invoke(
        app,
        [
            "snapshot",
            str(copy_fixture(tmp_path)),
            "--out",
            str(out),
            "--state",
            str(tmp_path / "state.db"),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 1, result.output
    assert "entry 2 of env:LLAMA_API_KEYS" in result.output
    assert "leakedsecretvalue" not in result.output
    assert not out.exists()


@pytest.mark.parametrize("command", ["deploy", "destroy", "snapshot", "controller"])
def test_commands_refuse_a_route_whose_key_source_yields_no_keys(
    command: str,
    empty_key_raw: dict,
    empty_key_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_serve(self: Controller, security: StreamSecurity, listen: str) -> None:
        return None

    monkeypatch.setattr(Controller, "run", never_serve)
    path = tmp_path / "multihull.yaml"
    path.write_text(yaml.safe_dump(empty_key_raw))
    out = tmp_path / "snapshot.json"
    args = {
        "deploy": ["deploy", str(path), "--snapshot-out", str(out)],
        "destroy": ["destroy", str(path), "--yes", "--snapshot-out", str(out)],
        "snapshot": ["snapshot", str(path), "--out", str(out)],
        "controller": ["controller", str(path), "--snapshot-out", str(out), "--insecure"],
    }[command]
    result = runner.invoke(
        app, [*args, "--state", str(tmp_path / "state.db")], env={"COLUMNS": "1000"}
    )
    assert result.exit_code == 1, result.output
    assert no_keys_message(empty_key_source) in result.output
    assert not out.exists()


def copy_docker_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "mock-three-docker.yaml", dest)
    return dest


def test_controller_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    spec = copy_docker_fixture(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    log = tmp_path / "controller.log"
    command = [
        sys.executable,
        "-m",
        "multihull.cli",
        "controller",
        str(spec),
        "--insecure",
        "--grpc-listen",
        "127.0.0.1:0",
        "--snapshot-out",
        str(snapshot),
        "--state",
        str(tmp_path / "state.db"),
    ]
    env = {**os.environ, "DOCKER_HOST": f"unix://{tmp_path / 'no-docker.sock'}"}
    with log.open("w") as output:
        process = subprocess.Popen(
            command, cwd=tmp_path, env=env, stdout=output, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 30
            while not snapshot.exists() and process.poll() is None:
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.1)
            assert process.poll() is None, log.read_text()
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 0, log.read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_plan_docker_fixture(tmp_path: Path) -> None:
    path = copy_docker_fixture(tmp_path)
    out = tmp_path / "plan"
    result = runner.invoke(
        app, ["plan", str(path), "--out", str(out), "--state", str(tmp_path / "state.db")]
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "docker-a.json",
        "docker-b.json",
        "docker-c.json",
    ]
    rendered = json.loads((out / "docker-a.json").read_text())
    assert rendered["name"] == "multihull-mock-three-docker-a"
    assert rendered["ports"] == {"8000/tcp": ["127.0.0.1", 18001]}
    assert "docker" in result.output and "note" not in result.output


def test_plan_prints_gpu_note_for_docker_target(tmp_path: Path) -> None:
    path = copy_docker_fixture(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["resources"]["gpu"] = ["L4"]
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = runner.invoke(
        app, ["plan", str(path), "--out", str(tmp_path / "plan"), "--state", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 0, result.output
    assert "note" in result.output and "ignored" in result.output


def test_doctor_docker_pings_daemon(tmp_path: Path, monkeypatch) -> None:
    path = copy_docker_fixture(tmp_path)
    reachable = FakeDockerClient()
    monkeypatch.setattr(dockermod, "default_client", lambda: reachable)
    result = runner.invoke(app, ["doctor", str(path)])
    assert result.exit_code == 0, result.output
    assert result.output.count("OK") == 3 and reachable.pings == 3

    monkeypatch.setattr(dockermod, "default_client", lambda: FakeDockerClient(reachable=False))
    result = runner.invoke(app, ["doctor", str(path)])
    assert result.exit_code == 1
    assert "FAIL" in result.output and "unreachable" in result.output


KIND_KUBECONFIG = """apiVersion: v1
kind: Config
clusters: [{name: kind, cluster: {server: 'https://127.0.0.1:6443'}}]
users: [{name: kind, user: {token: fixture}}]
contexts: [{name: kind-multihull-live, context: {cluster: kind, user: kind}}]
current-context: kind-multihull-live
"""


def test_doctor_fails_when_modal_rejects_credentials(tmp_path: Path, monkeypatch) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(KIND_KUBECONFIG)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-fixture-token-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-fixture-token-secret")
    fake = FakeModal()
    monkeypatch.setitem(sys.modules, "modal", fake)
    path = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "mock-kind-modal.yaml", path)

    accepted = runner.invoke(app, ["doctor", str(path)])
    assert accepted.exit_code == 0, accepted.output
    assert "FAIL" not in accepted.output and fake.client.hellos == 1

    fake.client.error = FakeAuthError("Token validation failed")
    rejected = runner.invoke(app, ["doctor", str(path)], env={"COLUMNS": "200"})
    assert rejected.exit_code == 1
    assert "FAIL" in rejected.output and "Token validation failed" in rejected.output
    assert "fixture-token" not in rejected.output


def test_doctor_tolerates_missing_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing-kubeconfig"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    path = copy_fixture(tmp_path)
    result = runner.invoke(app, ["doctor", str(path)])
    assert result.exit_code == 1
    assert "gke-prod" in result.output and "FAIL" in result.output
    assert "runpod-eu" in result.output


STATE_COMMANDS = {
    "plan": (["plan", "{spec}", "--out", "{tmp}/plan"], 0),
    "status": (["status", "{spec}"], 0),
    "deploy": (["deploy", "{spec}", "--snapshot-out", "{tmp}/snapshot.json"], 0),
    "destroy": (["destroy", "{spec}", "--yes", "--snapshot-out", "{tmp}/snapshot.json"], 0),
    "logs": (["logs", "{spec}", "--provider", "gke-prod"], 1),
    "controller": (
        ["controller", "{spec}", "--snapshot-out", "{tmp}/snapshot.json", "--insecure"],
        0,
    ),
    "snapshot": (["snapshot", "{spec}", "--out", "{tmp}/snapshot.json"], 0),
}


@pytest.fixture
def state_command(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_registry: dict[str, FakeProvider],
) -> tuple[list[str], int]:
    async def never_serve(self: Controller, security: StreamSecurity, listen: str) -> None:
        return None

    monkeypatch.setattr(Controller, "run", never_serve)
    monkeypatch.chdir(tmp_path)
    spec = copy_fixture(tmp_path)
    template, exit_code = STATE_COMMANDS[request.param]
    return [arg.format(spec=spec, tmp=tmp_path) for arg in template], exit_code


@pytest.mark.parametrize("state_command", sorted(STATE_COMMANDS), indirect=True)
def test_state_backend_comes_from_the_environment(
    state_command: tuple[list[str], int], tmp_path: Path
) -> None:
    args, exit_code = state_command
    from_env = tmp_path / "env" / "state.db"
    result = runner.invoke(app, args, env={"MULTIHULL_STATE_BACKEND": f"sqlite://{from_env}"})
    assert result.exit_code == exit_code, result.output
    assert from_env.exists()
    assert not (tmp_path / ".multihull" / "state.db").exists()


@pytest.mark.parametrize("state_command", sorted(STATE_COMMANDS), indirect=True)
def test_state_flag_overrides_the_environment(
    state_command: tuple[list[str], int], tmp_path: Path
) -> None:
    args, exit_code = state_command
    from_env = tmp_path / "env" / "state.db"
    from_flag = tmp_path / "flag" / "state.db"
    result = runner.invoke(
        app,
        [*args, "--state", str(from_flag)],
        env={"MULTIHULL_STATE_BACKEND": f"sqlite://{from_env}"},
    )
    assert result.exit_code == exit_code, result.output
    assert from_flag.exists()
    assert not from_env.exists()


@pytest.mark.parametrize("state_command", sorted(STATE_COMMANDS), indirect=True)
def test_state_defaults_to_the_local_file(
    state_command: tuple[list[str], int], tmp_path: Path
) -> None:
    args, exit_code = state_command
    result = runner.invoke(app, args, env={"MULTIHULL_STATE_BACKEND": None})
    assert result.exit_code == exit_code, result.output
    assert (tmp_path / ".multihull" / "state.db").exists()


@pytest.mark.parametrize("source", ["env", "flag"])
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("postgres://hull:hunter2@db.internal/multihull", "planned but not implemented yet"),
        ("redis://cache:6379/0", "unsupported state backend scheme 'redis'"),
    ],
)
@pytest.mark.parametrize("state_command", sorted(STATE_COMMANDS), indirect=True)
def test_bad_state_backend_exits_with_a_clear_error(
    state_command: tuple[list[str], int], tmp_path: Path, url: str, reason: str, source: str
) -> None:
    args, _ = state_command
    env = {"COLUMNS": "200", "MULTIHULL_STATE_BACKEND": url if source == "env" else None}
    flag = ["--state", url] if source == "flag" else []
    result = runner.invoke(app, [*args, *flag], env=env)
    assert result.exit_code == 2, result.output
    assert reason in result.output
    assert "hunter2" not in result.output and "Traceback" not in result.output
    assert not (tmp_path / ".multihull" / "state.db").exists()
