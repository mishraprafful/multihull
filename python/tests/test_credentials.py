from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from multihull import spec as specmod
from multihull.cli import app
from multihull.credentials import CredentialError, CredentialSource, read_token
from multihull.providers.runpod import API_KEY_ENV
from multihull.sdk import target as sdk_target
from multihull.spec import ServiceSpec

runner = CliRunner()
OVERRIDE_ENV = "MULTIHULL_TEST_OVERRIDE"


def sentinel() -> str:
    return secrets.token_hex(12)


def single_target_spec(llama_raw: dict[str, Any], provider: str, **target: Any) -> ServiceSpec:
    chosen = next(t for t in llama_raw["targets"] if t["provider"] == provider)
    llama_raw["targets"] = [{**chosen, **target}]
    llama_raw["reliability"]["minWarmProviders"] = 1
    llama_raw["container"]["secrets"] = []
    return ServiceSpec.model_validate(llama_raw)


def write_spec(tmp_path: Path, spec: ServiceSpec) -> Path:
    path = tmp_path / "multihull.yaml"
    path.write_text(yaml.safe_dump(specmod.dump(spec), sort_keys=False))
    return path


@pytest.mark.parametrize("reference", ["env:RUNPOD_PROD_KEY", "file:~/keys/runpod", "file:k"])
def test_spec_accepts_env_and_file_references(llama_raw: dict[str, Any], reference: str) -> None:
    spec = single_target_spec(llama_raw, "runpod-eu", credentials=reference)
    assert spec.targets[0].credentials == reference
    assert specmod.dump(spec)["targets"][0]["credentials"] == reference


def test_spec_without_credentials_keeps_native_locations(llama_spec: ServiceSpec) -> None:
    assert all(t.credentials is None for t in llama_spec.targets)
    assert all("credentials" not in t for t in specmod.dump(llama_spec)["targets"])


@pytest.mark.parametrize("prefix", ["", "env:", "env:1BAD", "file:", "vault:", "env:A B"])
def test_spec_rejects_other_shapes_without_echoing_them(
    llama_raw: dict[str, Any], prefix: str
) -> None:
    value = f"{prefix}{sentinel()}" if prefix in ("", "vault:") else prefix
    with pytest.raises(ValidationError) as raised:
        single_target_spec(llama_raw, "runpod-eu", credentials=value)
    message = str(raised.value)
    assert "targets.0.credentials" in message
    assert "credentials must be env:NAME" in message and "file:PATH" in message
    if prefix in ("", "vault:"):
        assert value not in message


def test_cli_validate_never_prints_a_pasted_value(
    llama_raw: dict[str, Any], tmp_path: Path
) -> None:
    pasted = sentinel()
    llama_raw["targets"][2]["credentials"] = pasted
    path = tmp_path / "multihull.yaml"
    path.write_text(yaml.safe_dump(llama_raw))
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "credentials must be env:NAME" in result.output
    assert pasted not in result.output


def test_json_schema_documents_the_reference_pattern() -> None:
    field = specmod.json_schema()["$defs"]["TargetSpec"]["properties"]["credentials"]
    pattern = next(option for option in field["anyOf"] if option.get("type") == "string")
    assert pattern["pattern"].startswith("^(env:")
    assert "env:NAME" in field["description"]


def test_sdk_targets_take_credentials() -> None:
    built = [
        sdk_target.kubernetes("k", 1, credentials="file:kubeconfig"),
        sdk_target.modal("m", 2, credentials="env:MODAL_PROD"),
        sdk_target.runpod("r", 3, credentials="env:RUNPOD_PROD"),
        sdk_target.baseten("b", 4, credentials="env:BASETEN_PROD"),
        sdk_target.replicate("p", 5, credentials="env:REPLICATE_PROD"),
        sdk_target.docker("d", 6, credentials="file:~/.docker/prod.json"),
    ]
    assert [t.credentials for t in built] == [
        "file:kubeconfig",
        "env:MODAL_PROD",
        "env:RUNPOD_PROD",
        "env:BASETEN_PROD",
        "env:REPLICATE_PROD",
        "file:~/.docker/prod.json",
    ]


def test_env_source_reads_the_named_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    value = sentinel()
    monkeypatch.setenv(OVERRIDE_ENV, f" {value}\n")
    assert CredentialSource.parse(f"env:{OVERRIDE_ENV}").read() == f" {value}\n"
    assert read_token(f"env:{OVERRIDE_ENV}", API_KEY_ENV) == value


@pytest.mark.parametrize("raw", [None, "", "  \n"])
def test_missing_or_empty_env_names_the_variable_only(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    if raw is None:
        monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    else:
        monkeypatch.setenv(OVERRIDE_ENV, raw)
    with pytest.raises(CredentialError) as raised:
        read_token(f"env:{OVERRIDE_ENV}", API_KEY_ENV)
    expected = "is not set" if raw is None else "is empty"
    assert str(raised.value) == f"credentials env:{OVERRIDE_ENV} {expected}"


def test_file_source_expands_home_and_resolves_relative_to_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    (home / "keys").mkdir(parents=True)
    work.mkdir()
    in_home, in_work = sentinel(), sentinel()
    (home / "keys" / "runpod").write_text(in_home + "\n")
    (work / "runpod").write_text(in_work + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    assert read_token("file:~/keys/runpod", API_KEY_ENV) == in_home
    assert read_token("file:runpod", API_KEY_ENV) == in_work
    assert str(CredentialSource.parse("file:runpod")) == f"file:{work / 'runpod'}"


def test_unreadable_file_names_the_path_only(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(CredentialError) as raised:
        read_token(f"file:{missing}", API_KEY_ENV)
    assert (
        str(raised.value) == f"credentials file:{missing} is unreadable: No such file or directory"
    )
    with pytest.raises(CredentialError) as raised:
        read_token(f"file:{tmp_path}", API_KEY_ENV)
    assert str(raised.value).startswith(f"credentials file:{tmp_path} is unreadable")
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe" + sentinel().encode())
    with pytest.raises(CredentialError) as raised:
        read_token(f"file:{binary}", API_KEY_ENV)
    assert str(raised.value) == f"credentials file:{binary} is not UTF-8 text"


def test_native_location_applies_without_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    value = sentinel()
    monkeypatch.setenv(API_KEY_ENV, value)
    assert read_token(None, API_KEY_ENV) == value
    monkeypatch.delenv(API_KEY_ENV)
    with pytest.raises(CredentialError, match=f"^{API_KEY_ENV} is not set$"):
        read_token(None, API_KEY_ENV)
