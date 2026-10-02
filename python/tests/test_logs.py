from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from multihull import logs as logsmod
from multihull.cli import app
from multihull.durations import format_duration, parse_duration
from multihull.providers.base import Ref
from multihull.providers.modal import ModalProvider
from multihull.state import LocalState, StateRecord
from tests.conftest import FIXTURES
from tests.fakes import FakeProvider

runner = CliRunner()


def test_stream_logs_follow_polls(tmp_path: Path) -> None:
    provider = FakeProvider(log_lines=["one", "two"])
    ref = Ref("gke-prod", "kubernetes", "llama-8b", {"id": "1"})
    slept: list[float] = []
    lines = list(
        logsmod.stream_logs(
            provider,
            ref,
            timedelta(minutes=5),
            follow=True,
            poll_interval=1.5,
            max_polls=2,
            sleep=slept.append,
        )
    )
    assert lines == ["one", "two"] * 3
    assert provider.log_calls == [
        timedelta(minutes=5),
        timedelta(seconds=1.5),
        timedelta(seconds=1.5),
    ]
    assert slept == [1.5, 1.5]
    assert list(logsmod.stream_logs(provider, ref, timedelta(minutes=1))) == ["one", "two"]


def test_logs_cli(tmp_path: Path, fake_registry: dict[str, FakeProvider]) -> None:
    fake_registry["kubernetes"].log_lines = ["pod-a hello", "pod-a world"]
    path = tmp_path / "multihull.yaml"
    shutil.copy(FIXTURES / "llama-8b.yaml", path)
    state_path = tmp_path / "state.db"
    base = [str(path), "--state", str(state_path)]

    missing = runner.invoke(app, ["logs", *base, "-p", "gke-prod"])
    assert missing.exit_code == 1 and "no state" in missing.output

    ref = Ref("gke-prod", "kubernetes", "llama-8b", {"namespace": "inference"})
    LocalState(state_path).put(StateRecord("llama-8b", "gke-prod", ref.to_json(), None, "h"))
    result = runner.invoke(app, ["logs", *base, "-p", "gke-prod", "--since", "5m"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["pod-a hello", "pod-a world"]
    assert fake_registry["kubernetes"].log_calls == [timedelta(minutes=5)]

    assert runner.invoke(app, ["logs", *base]).exit_code == 2
    assert runner.invoke(app, ["logs", *base, "-p", "nope"]).exit_code == 2
    assert runner.invoke(app, ["logs", *base, "-p", "gke-prod", "--since", "x"]).exit_code == 2


def test_modal_logs_without_sdk_is_clear_error(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "modal" or name.startswith("modal."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    provider = ModalProvider(dry_run=False)
    ref = Ref("modal-main", "modal", "llama-8b", {"app": "multihull-llama-8b"})
    try:
        list(provider.logs(ref, timedelta(minutes=1)))
    except RuntimeError as exc:
        assert "modal SDK not installed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert list(ModalProvider(dry_run=True).logs(ref, timedelta(minutes=1))) == []


def test_parse_and_format_duration() -> None:
    assert parse_duration("30s") == timedelta(seconds=30)
    assert parse_duration("10m") == timedelta(minutes=10)
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("1.5m") == timedelta(seconds=90)
    assert format_duration(timedelta(seconds=30)) == "30s"
    assert format_duration(timedelta(minutes=10)) == "10m"
    assert format_duration(timedelta(hours=2)) == "2h"
    assert format_duration(timedelta(seconds=90)) == "90s"
    for bad in ("", "10", "ten minutes", "5w"):
        try:
            parse_duration(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
