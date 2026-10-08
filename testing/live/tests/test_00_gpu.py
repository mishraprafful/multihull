from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from live.gpu import (
    BUDGET_USD,
    DEFAULT_MAX_MINUTES,
    MAX_MINUTES_ENV,
    Budget,
    BudgetExceeded,
    ejected,
    max_minutes_from_env,
    new_api_key,
    new_cost,
    running_apps,
    served_model,
    worst_case_usd,
)
from live.summary import ModalInfo, ModalState, RunSummary, publish, render
from multihull.apikeys import API_KEY_PATTERN

RUNNING_APPS: list[dict[str, Any]] = [
    {
        "app_id": "ap-A",
        "description": "multihull-live-7-modal-a",
        "state": "stopped",
        "created_at": "2026-10-08 09:00:00+00:00",
    },
    {
        "app_id": "ap-B",
        "description": "multihull-live-7-modal-b",
        "state": "deployed",
        "created_at": "2026-10-08 09:00:05+00:00",
    },
    {
        "app_id": "ap-C",
        "description": "multihull-live-8-modal-a",
        "state": "deployed",
        "created_at": "2026-10-08 09:00:05+00:00",
    },
]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_budget_bounds_waits_and_fails_once_exceeded() -> None:
    clock = FakeClock()
    budget = Budget(2, clock)
    assert budget.bounded(900) == 120
    clock.now += 100
    assert budget.bounded(900) == 20
    assert not budget.exceeded
    budget.check()
    clock.now += 25
    assert budget.exceeded
    assert budget.bounded(900) == 0
    with pytest.raises(BudgetExceeded, match="max_minutes 2 exceeded"):
        budget.check()


def test_max_minutes_comes_from_the_environment() -> None:
    assert max_minutes_from_env({}) == DEFAULT_MAX_MINUTES
    assert max_minutes_from_env({MAX_MINUTES_ENV: " 7 "}) == 7
    with pytest.raises(ValueError, match="at least 1"):
        max_minutes_from_env({MAX_MINUTES_ENV: "0"})


def test_served_model_prefers_the_served_name() -> None:
    command = ["python3", "-m", "x", "--model", "Qwen/Q", "--served-model-name", "q"]
    assert served_model({"container": {"command": command}}) == "q"
    assert served_model({"container": {"command": command[:5]}}) == "Qwen/Q"
    with pytest.raises(ValueError, match="names no model"):
        served_model({"container": {"command": ["python3"]}})


def test_api_keys_match_the_route_key_format() -> None:
    assert API_KEY_PATTERN.fullmatch(new_api_key())
    assert new_api_key() != new_api_key()


def test_worst_case_with_both_targets_up_for_the_job_timeout_stays_under_budget() -> None:
    assert worst_case_usd(2, 40) < BUDGET_USD


def test_running_apps_lists_only_unstopped_apps_with_the_prefix() -> None:
    assert running_apps("multihull-live-7", "main", lambda env: RUNNING_APPS) == [
        "multihull-live-7-modal-b"
    ]
    assert running_apps("multihull-live-9", "main", lambda env: RUNNING_APPS) == []


def test_ejected_means_not_ready_probe_down_or_circuit_open() -> None:
    assert not ejected({"health": "ready", "circuit": "closed", "probe": {"state": "up"}})
    assert ejected({"health": "ready", "circuit": "open", "probe": {"state": "up"}})
    assert ejected({"health": "ready", "circuit": "closed", "probe": {"state": "down"}})
    assert ejected({"health": "draining", "circuit": None, "probe": None})


def gpu_summary() -> RunSummary:
    summary = RunSummary(spec="gpu-modal", service="live-7", exit_status=0)
    summary.cost = new_cost(2, 20)
    summary.cost.started_at = 1_000.0
    summary.cost.stopped_at = 1_000.0 + 20 * 60
    for provider in ("modal-a", "modal-b"):
        summary.modal_apps.append(
            ModalInfo(
                app_name=f"multihull-live-7-{provider}",
                environment="main",
                provider=provider,
                web_url=f"https://acme--multihull-live-7-{provider}.modal.run",
                states=[ModalState(label="after deploy", state="deployed", tasks=1)],
            )
        )
    summary.destroy = "hull destroy exit 0, state records left: none"
    return summary


def test_cost_line_uses_wall_clock_gpu_minutes_and_names_the_budget() -> None:
    text = render(gpu_summary(), "success")
    assert "### Cost" in text
    assert "About 0.53 USD: 2 x L4 for 20.0 wall-clock minutes" in text
    assert "(40.0 GPU minutes) at 0.80 USD per GPU hour (https://modal.com/pricing, read" in text
    assert "Budget: 5 USD per run, within budget" in text
    assert "after 20 minutes (`max_minutes`)" in text
    assert "### Kubernetes (kind)" not in text
    assert "### Modal (modal-a)" in text and "### Modal (modal-b)" in text
    assert text.count("- Suite destroy: hull destroy exit 0") == 1
    assert text.index("### Modal (modal-b)") < text.index("- Suite destroy")


def test_publish_closes_an_open_cost_window_and_marks_an_over_budget_run(
    tmp_path: Path,
) -> None:
    summary = gpu_summary()
    assert summary.cost is not None
    summary.cost.stopped_at = None
    summary.cost.started_at = 0.0
    summary.save(tmp_path)

    text = publish(tmp_path, "failure", "live-gpu-logs", "gpu-modal", "live-7")

    assert "OVER budget" in text
    assert "Logs: artifact `live-gpu-logs`" in text
    reloaded = RunSummary.load(tmp_path)
    assert reloaded is not None and reloaded.cost is not None
    assert reloaded.cost.stopped_at is None


def test_cost_without_a_started_container_bills_only_the_build() -> None:
    summary = gpu_summary()
    assert summary.cost is not None
    summary.cost.started_at = None
    text = render(summary, "failure")
    assert "No GPU container started" in text
    assert "Budget: 5 USD per run." in text
