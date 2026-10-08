from __future__ import annotations

import pytest

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import Router, endpoint_id

TUNING = {
    "timeouts": {"connect": 1.5, "first_byte": 3, "idle": 45, "total": 120},
    "circuit": {
        "consecutive_failures": 3,
        "error_ratio": 0.4,
        "ratio_window": 20,
        "min_samples": 10,
        "base_backoff": 2,
        "max_backoff": 60,
        "jitter_fraction": 0.1,
        "half_open_ramp": 15,
        "probe_successes_to_close": 2,
        "provider_open_ratio": 0.75,
        "panic_threshold": 0.6,
    },
    "admission": {"max_wait": 3, "bound": 256},
    "pressure": {
        "queue_wait_fraction": 0.4,
        "sustained": 1,
        "resend_every": 4,
        "stale_after": 8,
        "ttft_degrade_factor": 3.0,
        "ttft_window": 20,
        "ttft_baseline_smoothing": 0.3,
        "ttft_rebaseline_after": 900,
    },
    "probe": {
        "enabled": True,
        "interval": 2.5,
        "timeout": 1,
        "jitter_fraction": 0.1,
        "failure_threshold": 2,
        "success_threshold": 4,
    },
    "retry": {"budget_ratio": 0.3, "budget_window": 20, "min_retries_per_second": 4},
}


@pytest.mark.router_tuning(**TUNING)
def test_debug_config_reflects_every_tuning_table(router: Router, client: RouterClient) -> None:
    config = router.config()
    for table, expected in TUNING.items():
        assert config[table] == expected, table
    assert config["listen"] == f"127.0.0.1:{router.listen_port}"
    assert config["max_buffered_body_bytes"] == 1024 * 1024

    outcomes = load(client, 5, stream=False, concurrency=1)
    assert failures(outcomes) == []
    assert providers_of(outcomes) == {"primary": 5}
    assert router.metrics().gauge("router_circuit_state", endpoint=endpoint_id("primary")) == 0


def test_invalid_tuning_is_rejected_on_startup(router_binary, router_source, tmp_path) -> None:
    broken = Router(
        router_binary,
        tmp_path / "router.toml",
        tmp_path / "router.log",
        router_source,
        tuning={"circuit": {"error_ratio": 1.5}},
    )
    broken.start()
    try:
        assert broken.popen is not None
        assert broken.popen.wait(timeout=10) != 0
        assert "circuit.error_ratio" in broken.log_text()
    finally:
        broken.stop()
