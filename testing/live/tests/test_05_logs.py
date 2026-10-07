from __future__ import annotations

import pytest

from live.capture import ScenarioProbe
from live.harness import PRIMARY, LiveDeployment


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_hull_logs_reads_each_target(
    deployment: LiveDeployment, scenario: ScenarioProbe, role: str
) -> None:
    provider = PRIMARY if role == "primary" else deployment.secondary
    result = deployment.logs(provider)
    lines = result.stdout.splitlines()
    scenario.note(f"hull logs -p {provider}: exit {result.returncode}, {len(lines)} lines")

    assert result.returncode == 0, result.stderr
    assert lines, f"no log lines from {provider}"
