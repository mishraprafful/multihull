from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from live import sweep
from live.sweep import main, stale_apps

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def app(name: str, state: str, created: str) -> dict[str, str]:
    return {"app_id": f"ap-{name}", "description": name, "state": state, "created_at": created}


def test_sweeper_picks_only_old_running_live_apps() -> None:
    apps = [
        app("multihull-live-1", "deployed", "2026-10-07 09:00:00+00:00"),
        app("multihull-live-2", "deployed", "2026-10-07 11:30:00+00:00"),
        app("multihull-live-3", "stopped", "2026-10-07 08:00:00+00:00"),
        app("multihull-prod", "deployed", "2026-10-01 08:00:00+00:00"),
        app("multihull-live-4", "initializing...", "2026-10-07 11:00:00+02:00"),
    ]
    stale = stale_apps(apps, "multihull-live-", timedelta(hours=2), NOW)
    assert [a["description"] for a in stale] == ["multihull-live-1", "multihull-live-4"]
    current = stale_apps(apps, "multihull-live-2", timedelta(0), NOW)
    assert [a["description"] for a in current] == ["multihull-live-2"]


def test_sweeper_refuses_prefixes_outside_live_runs() -> None:
    with pytest.raises(SystemExit):
        main(["--prefix", "multihull-"])


def test_sweeper_reports_a_failed_listing_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rejected(environment: str) -> list[dict[str, str]]:
        raise RuntimeError("modal app list failed: Token validation failed")

    monkeypatch.setattr(sweep, "list_apps", rejected)
    assert main(["--prefix", "multihull-live-1"]) == 1
    assert capsys.readouterr().out == "skipped, modal app list failed: Token validation failed\n"


def test_check_fails_while_an_app_of_the_run_still_runs_and_stops_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = [
        app("multihull-live-7-modal-a", "deployed", "2026-10-07 09:00:00+00:00"),
        app("multihull-live-7-modal-b", "stopped", "2026-10-07 09:00:00+00:00"),
        app("multihull-live-8-modal-a", "deployed", "2026-10-07 09:00:00+00:00"),
    ]
    monkeypatch.setattr(sweep, "list_apps", lambda environment: listed)

    def refuse(*args: str) -> None:
        raise AssertionError(f"check must not run modal {args}")

    monkeypatch.setattr(sweep, "modal", refuse)
    assert main(["--prefix", "multihull-live-7", "--check"]) == 1
    assert capsys.readouterr().out == (
        "::error title=Left after the run::Modal main: multihull-live-7-modal-a still running\n"
    )
    assert main(["--prefix", "multihull-live-9", "--check"]) == 0
    assert capsys.readouterr().out == "no running Modal app named multihull-live-9*\n"


def test_check_fails_when_it_cannot_list_the_apps(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rejected(environment: str) -> list[dict[str, str]]:
        raise RuntimeError("modal app list failed: Token validation failed")

    monkeypatch.setattr(sweep, "list_apps", rejected)
    assert main(["--prefix", "multihull-live-1", "--check"]) == 1
    assert "Modal not checked: modal app list failed" in capsys.readouterr().out
